"""Resource tools preserve resource behavior across HTTP interface modes."""

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import parse_qs, quote, urlsplit

import aiohttp
import pytest
from aiohttp.test_utils import TestClient, TestServer
from pydantic import BaseModel
from test_http_sse import OldClient
from test_resource_scopes import registry_with_resources
from test_subscriptions import event, message

from aiohttp_tiny_mcp import Client, Endpoint, Extension, Registry, SseEndpoint
from aiohttp_tiny_mcp.client.base import ClientError
from aiohttp_tiny_mcp.protocol.core import Operation
from aiohttp_tiny_mcp.protocol.models import CallToolResult, ListParams, TextContent
from aiohttp_tiny_mcp.protocol.selection import AdapterSet
from aiohttp_tiny_mcp.storage.hub import NOTIFICATIONS, topic
from aiohttp_tiny_mcp.testing import connect, pick, serving

VERSIONS = tuple(AdapterSet.default().by_version)
RESOURCE_TOOLS = {"mcp_resources_list", "mcp_resources_read"}


class Nothing(BaseModel):
    pass


class Entry(BaseModel):
    name: str


def resource_registry(**kwargs: Any) -> Registry:
    registry = Registry("resource-tools", "1", **kwargs)

    @registry.resource("file://alpha", mime_type="text/plain")
    async def alpha(args: Nothing) -> str:
        return "alpha content"

    @registry.resource("file://binary", mime_type="application/octet-stream")
    async def binary(args: Nothing) -> bytes:
        return b"\x00\xff"

    @registry.resource("file://entries/{name}")
    async def entry(args: Entry) -> str:
        return f"entry {args.name}"

    return registry


@asynccontextmanager
async def resource_client(
    registry: Registry,
    query: str = "resources=tool",
    version: str = "2026-07-28",
    headers: dict[str, str] | None = None,
) -> AsyncIterator[tuple[Client, dict[str, Any]]]:
    async with serving(registry) as url:
        async with aiohttp.ClientSession(headers=headers) as session:
            async with Client(
                url + ("?" + query if query else ""), pick(version), session=session
            ) as client:
                yield client, await client.initialize()


def tool_data(result: CallToolResult | dict[str, Any]) -> dict[str, Any]:
    assert isinstance(result, CallToolResult)
    assert not result.is_error
    assert len(result.content) == 1
    assert isinstance(result.content[0], TextContent)
    return json.loads(result.content[0].text)


@pytest.mark.parametrize("version", VERSIONS)
@pytest.mark.parametrize(
    "query,native,tools",
    [
        ("", True, False),
        ("resources=native", True, False),
        ("resources=tool", False, True),
        ("resources=tool&resources=native", True, True),
        ("resources=native&resources=tool&resources=tool", True, True),
    ],
)
async def test_resource_interface_modes(version: str, query: str, native: bool, tools: bool):
    async with resource_client(resource_registry(), query, version) as (client, discovery):
        capabilities = discovery["capabilities"]
        assert ("resources" in capabilities) is native
        assert ("tools" in capabilities) is tools
        assert {tool.name for tool in await client.list_tools()} == (
            RESOURCE_TOOLS if tools else set()
        )
        if native:
            assert {resource.uri for resource in await client.list_resources()} == {
                "file://alpha",
                "file://binary",
            }
        else:
            with pytest.raises(ClientError) as error:
                await client.list_resources()
            assert error.value.code == -32601
            with pytest.raises(ClientError) as error:
                await client.read_resource("file://alpha")
            assert error.value.code == -32601
            with pytest.raises(ClientError) as error:
                await client.request(Operation.LIST_RESOURCE_TEMPLATES, ListParams())
            assert error.value.code == -32601
        if tools:
            result = tool_data(
                await client.call_tool("mcp_resources_read", {"uri": "file://alpha"})
            )
            assert result["contents"][0]["text"] == "alpha content"
        else:
            with pytest.raises(ClientError):
                await client.call_tool("mcp_resources_read", {"uri": "file://alpha"})


@pytest.mark.parametrize("version", VERSIONS)
async def test_resource_tools_preserve_pagination_templates_and_contents(version: str):
    async with resource_client(resource_registry(page_size=1), version=version) as (
        client,
        discovery,
    ):
        first = tool_data(await client.call_tool("mcp_resources_list", {}))
        assert [resource["uri"] for resource in first["resources"]] == ["file://alpha"]
        second = tool_data(
            await client.call_tool("mcp_resources_list", {"cursor": first["nextCursor"]})
        )
        assert [resource["uri"] for resource in second["resources"]] == ["file://binary"]
        assert not second.get("nextCursor")
        templates = tool_data(await client.call_tool("mcp_resources_list", {"kind": "templates"}))
        assert templates["resourceTemplates"][0]["uriTemplate"] == "file://entries/{name}"
        text = tool_data(
            await client.call_tool("mcp_resources_read", {"uri": "file://entries/report"})
        )
        assert text["contents"][0]["uri"] == "file://entries/report"
        assert text["contents"][0]["text"] == "entry report"
        binary = tool_data(await client.call_tool("mcp_resources_read", {"uri": "file://binary"}))
        assert binary["contents"][0]["uri"] == "file://binary"
        assert binary["contents"][0]["mimeType"] == "application/octet-stream"
        assert binary["contents"][0]["blob"] == "AP8="


@pytest.mark.parametrize(
    "name,arguments",
    [
        ("mcp_resources_read", {}),
        ("mcp_resources_read", {"uri": "file://missing"}),
        ("mcp_resources_list", {"kind": "unknown"}),
        ("mcp_resources_list", {"cursor": "missing"}),
    ],
)
async def test_resource_tool_errors_are_tool_results(name: str, arguments: dict[str, Any]):
    async with resource_client(resource_registry()) as (client, discovery):
        result = await client.call_tool(name, arguments)
        assert isinstance(result, CallToolResult)
        assert result.is_error
        assert isinstance(result.content[0], TextContent)
        assert result.content[0].text


@pytest.mark.parametrize("version", VERSIONS)
@pytest.mark.parametrize("authorized", [False, True])
@pytest.mark.parametrize("uri", ["files://config", "files://entries/42"])
async def test_resource_tool_scopes_precede_dependency_resolution(
    version: str, authorized: bool, uri: str
):
    registry, calls = registry_with_resources({"files:read", "profile:read"} if authorized else ())
    async with resource_client(
        registry, version=version, headers={"Authorization": "Bearer test-token"}
    ) as (client, discovery):
        result = await client.call_tool("mcp_resources_read", {"uri": uri})
        assert isinstance(result, CallToolResult)
        assert result.is_error is not authorized
        if authorized:
            assert calls[0] == "dependency"
            assert len(calls) == 2
        else:
            assert calls == []
            assert "files:read" in result.content[0].text


@pytest.mark.parametrize("name", sorted(RESOURCE_TOOLS))
def test_resource_tool_names_are_reserved_by_default(name: str):
    registry = Registry("reserved", "1")

    async def user_tool(args: Nothing) -> str:
        return "user tool"

    with pytest.raises(ValueError, match="reserved"):
        registry.tool(user_tool, name=name)
    registry.tool(user_tool, name="mcp_resources_custom")


@pytest.mark.parametrize("query", ["resources=tool", "resources=unknown", "resources="])
async def test_opt_out_disables_selection_and_releases_names(query: str):
    registry = resource_registry(resource_tools=False)

    async def user_tool(args: Nothing) -> str:
        return "user tool"

    for name in RESOURCE_TOOLS:
        registry.tool(user_tool, name=name)
    async with resource_client(registry, query) as (client, discovery):
        assert "resources" in discovery["capabilities"]
        assert len(await client.list_resources()) == 2
        assert (await client.read_resource("file://alpha")).contents[0].text == "alpha content"
        for name in RESOURCE_TOOLS:
            result = await client.call_tool(name, {})
            assert isinstance(result, CallToolResult)
            assert not result.is_error
            assert result.content[0].text == "user tool"


@pytest.mark.parametrize(
    "query", ["resources=unknown", "resources=", "resources=native&resources=invalid"]
)
async def test_invalid_resource_mode_returns_http_400(query: str):
    async with TestClient(TestServer(Endpoint(resource_registry()).app())) as client:
        response = await client.post(
            "/mcp?" + query,
            json=message(1, "server/discover"),
            headers={"MCP-Protocol-Version": "2026-07-28", "Mcp-Method": "server/discover"},
        )
        assert response.status == 400
        assert "resources" in await response.text()


async def test_selection_is_request_local():
    async with resource_client(resource_registry()) as (client, discovery):
        assert {tool.name for tool in await client.list_tools()} == RESOURCE_TOOLS
        client.base_url = client.base_url.split("?", 1)[0]
        assert await client.list_tools() == []
        assert len(await client.list_resources()) == 2
        client.base_url += "?resources=tool"
        assert {tool.name for tool in await client.list_tools()} == RESOURCE_TOOLS


async def test_opt_out_leaves_resource_only_registry_without_tools():
    async with resource_client(resource_registry(resource_tools=False)) as (client, discovery):
        assert "tools" not in discovery["capabilities"]
        assert "resources" in discovery["capabilities"]
        assert await client.list_tools() == []
        with pytest.raises(ClientError):
            await client.call_tool("mcp_resources_read", {"uri": "file://alpha"})


@pytest.mark.parametrize("query", ["", "resources=tool", "resources=tool&resources=native"])
async def test_resource_modes_preserve_user_tools(query: str):
    registry = resource_registry(page_size=1)

    @registry.tool
    async def user_tool(args: Nothing) -> str:
        return "ordinary tool"

    async with resource_client(registry, query) as (client, discovery):
        assert "tools" in discovery["capabilities"]
        expected = {"user_tool"} | (RESOURCE_TOOLS if "tool" in query else set())
        assert {tool.name for tool in await client.list_tools()} == expected
        result = await client.call_tool("user_tool", {})
        assert isinstance(result, CallToolResult)
        assert result.content[0].text == "ordinary tool"


@pytest.mark.parametrize("version", VERSIONS)
async def test_stdio_keeps_native_resources_without_resource_tools(version: str):
    async with connect(resource_registry(), adapter=version, initialize=False) as client:
        discovery = await client.initialize()
        assert "resources" in discovery["capabilities"]
        assert "tools" not in discovery["capabilities"]
        assert await client.list_tools() == []
        assert (await client.read_resource("file://alpha")).contents[0].text == "alpha content"


async def open_legacy_resources(client: OldClient, query: str) -> str:
    client.stream = await client.http.get(
        f"{client.base}/sse?{query}", headers={"Accept": "text/event-stream"}
    )
    kind, endpoint = await client.next_event()
    assert kind == "endpoint"
    client.post_to = endpoint
    client.reading = asyncio.create_task(client.read())
    return endpoint


@pytest.mark.parametrize("query", ["resources=tool", "resources=tool&resources=native"])
async def test_legacy_sse_preserves_resource_selection_in_post_endpoint(query: str):
    registry = resource_registry()
    app = Endpoint(registry).app()
    SseEndpoint(registry).setup(app)
    async with TestServer(app) as server:
        client = OldClient(str(server.make_url("")).rstrip("/"))
        try:
            endpoint = await open_legacy_resources(client, query)
            assert parse_qs(urlsplit(endpoint).query)["resources"] == parse_qs(query)["resources"]
            discovery = await client.call(
                "initialize",
                {
                    "protocolVersion": "2024-11-05",
                    "clientInfo": {"name": "resource-tools-test", "version": "1"},
                    "capabilities": {},
                },
            )
            native = "native" in query
            assert ("resources" in discovery["result"]["capabilities"]) is native
            listed = await client.call("tools/list", {}, request_id=2)
            assert {tool["name"] for tool in listed["result"]["tools"]} == RESOURCE_TOOLS
            read = await client.call(
                "tools/call",
                {"name": "mcp_resources_read", "arguments": {"uri": "file://alpha"}},
                request_id=3,
            )
            assert (
                json.loads(read["result"]["content"][0]["text"])["contents"][0]["text"]
                == "alpha content"
            )
            direct = await client.call("resources/read", {"uri": "file://alpha"}, request_id=4)
            if native:
                assert direct["result"]["contents"][0]["text"] == "alpha content"
            else:
                assert direct["error"]["code"] == -32601
        finally:
            await client.close()


async def resource_notifications(registry: Registry) -> None:
    for method, params in [
        ("notifications/resources/list_changed", {}),
        ("notifications/resources/updated", {"uri": "file://alpha"}),
        ("notifications/tools/list_changed", {}),
    ]:
        await registry.hub.publish(
            topic(NOTIFICATIONS), {"jsonrpc": "2.0", "method": method, "params": params}
        )


async def test_legacy_tool_stream_filters_native_resource_notifications():
    registry = resource_registry()
    app = Endpoint(registry).app()
    SseEndpoint(registry).setup(app)
    async with TestServer(app) as server:
        client = OldClient(str(server.make_url("")).rstrip("/"))
        try:
            await open_legacy_resources(client, "resources=tool")
            client.post_to = client.post_to.replace("&resources=tool", "")
            subscribed = await client.call("resources/subscribe", {"uri": "file://alpha"})
            assert "result" in subscribed
            await resource_notifications(registry)
            notification = await asyncio.wait_for(client.seen.get(), 2)
            assert notification["method"] == "notifications/tools/list_changed"
        finally:
            await client.close()


async def test_modern_tool_stream_filters_native_resource_notifications():
    registry = resource_registry()
    async with TestClient(TestServer(Endpoint(registry).app())) as client:
        stream = await client.post(
            "/mcp?resources=tool",
            json=message(
                1,
                notifications={
                    "toolsListChanged": True,
                    "resourcesListChanged": True,
                    "resourceSubscriptions": ["file://alpha"],
                },
            ),
            headers={
                "Accept": "text/event-stream",
                "MCP-Protocol-Version": "2026-07-28",
                "Mcp-Method": "subscriptions/listen",
            },
        )
        try:
            assert stream.status == 200
            acknowledgement = await event(stream)
            assert acknowledgement["params"]["notifications"] == {"toolsListChanged": True}
            await resource_notifications(registry)
            assert (await event(stream))["method"] == "notifications/tools/list_changed"
        finally:
            stream.close()


@pytest.mark.parametrize("version", [version for version in VERSIONS if version != "2026-07-28"])
async def test_resource_read_tool_executes_legacy_extension_method_alias(version: str):
    registry = Registry("extension-tools", "1")
    extension = Extension("example.org/resource-tools")
    calls: list[str] = []

    @extension.method("entries/remove")
    async def remove(args: Entry) -> dict[str, str]:
        calls.append(args.name)
        return {"removed": args.name}

    registry.extension(extension)
    uri = "mcp-extensions://example.org/resource-tools/entries/remove?params=" + quote(
        json.dumps({"name": "report"}), safe=""
    )
    async with resource_client(registry, version=version) as (client, discovery):
        tools = {tool.name: tool for tool in await client.list_tools()}
        assert not (tools["mcp_resources_read"].annotations or {}).get("readOnlyHint", False)
        result = tool_data(await client.call_tool("mcp_resources_read", {"uri": uri}))
        assert result["contents"][0]["uri"] == uri
        assert json.loads(result["contents"][0]["text"]) == {"removed": "report"}
    assert calls == ["report"]
