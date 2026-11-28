"""Resource tools preserve resource behavior across HTTP interface modes."""

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import parse_qs, quote, urlsplit

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from pydantic import BaseModel

from aiohttp_tiny_mcp import (
    Authorization,
    Client,
    Endpoint,
    Exchange,
    Extension,
    Icon,
    Principal,
    Registry,
    SseEndpoint,
    StaticVerifier,
    elicit,
)
from aiohttp_tiny_mcp.client.base import ClientError
from aiohttp_tiny_mcp.protocol.core import Failure, FailureKind, Operation, Rejected
from aiohttp_tiny_mcp.protocol.models import (
    CallToolResult,
    ListParams,
    ReadResourceParams,
    TextContent,
)
from aiohttp_tiny_mcp.protocol.selection import AdapterSet
from aiohttp_tiny_mcp.storage.hub import NOTIFICATIONS, topic
from aiohttp_tiny_mcp.testing import answering, connect, pick, serving
from tests.helpers import OldClient
from tests.server.resources.test_resource_scopes import registry_with_resources
from tests.server.resources.test_subscriptions import event, message

VERSIONS = tuple(AdapterSet.default().by_version)
RESOURCE_TOOLS = {"mcp_resources_list", "mcp_resources_read"}


class Nothing(BaseModel):
    pass


class Entry(BaseModel):
    name: str


class File(Entry):
    path: str


async def described_resource(args: Nothing) -> str:
    """A resource with something to show beside its address."""
    return "alpha content"


async def entry_resource(args: Entry) -> str:
    """One entry by name."""
    return f"entry {args.name}"


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
    answers: dict[str, Any] | None = None,
) -> AsyncIterator[tuple[Client, dict[str, Any]]]:
    async with serving(registry) as url:
        async with aiohttp.ClientSession(headers=headers) as session:
            async with Client(
                url + ("?" + query if query else ""),
                pick(version),
                session=session,
                on_ask=answering(answers),
            ) as client:
                yield client, await client.initialize()


def tool_data(result: CallToolResult | dict[str, Any]) -> dict[str, Any]:
    assert isinstance(result, CallToolResult)
    assert not result.is_error
    assert len(result.content) == 1
    assert isinstance(result.content[0], TextContent)
    return json.loads(result.content[0].text)


@pytest.mark.parametrize("version", VERSIONS)
async def test_prompt_addresses_use_the_native_handler(version):
    registry = Registry("prompts", "1")

    @registry.prompt(name="Review / code")
    async def review(args: Entry) -> str:
        return f"Review {args.name}"

    query = "resources=tool&resources=native&prompts=resource"
    async with resource_client(registry, query=query, version=version) as (client, discovery):
        assert "resources" in discovery["capabilities"]
        assert await client.list_resources() == []
        (template,) = await client.list_resource_templates()
        listed = tool_data(await client.call_tool("mcp_resources_list", {"templates": True}))
        assert listed["items"][0]["uriTemplate"] == template.uri_template
        assert listed["items"][0]["arguments"] == [{"name": "name", "required": True}]
        args = {"name": "a + b / код"}
        uri = template.uri_template.replace("{arguments}", quote(json.dumps(args), safe=""))
        prompt = await client.get_prompt("Review / code", args)
        expected = prompt.model_dump(mode="json", by_alias=True, include={"messages"})
        assert tool_data(await client.call_tool("mcp_resources_read", {"uri": uri})) == expected
        result = await client.read_resource(uri)
        assert json.loads(result.contents[0].text) == expected

        address = template.uri_template.split("?", 1)[0]
        for uri in (
            address,
            address + "?arguments=%5B%5D",
            address + "?arguments=%7B%7D&arguments=%7B%7D",
        ):
            rejected = await client.call_tool("mcp_resources_read", {"uri": uri})
            assert isinstance(rejected, CallToolResult) and rejected.is_error
            with pytest.raises(ClientError):
                await client.read_resource(uri)


@pytest.mark.parametrize("version", VERSIONS)
async def test_prompt_tool_read_can_ask(version):
    registry = Registry("prompts", "1")

    @registry.prompt
    async def topic(args: Nothing, ex: Exchange) -> str:
        answer = await ex.ask("topic", elicit("Choose a topic", Entry.model_json_schema()))
        return f"Review {answer.content['name']}"

    async with resource_client(
        registry, version=version, answers={"topic": {"name": "Python"}}
    ) as (client, discovery):
        assert "resources" not in discovery["capabilities"]
        (item,) = tool_data(await client.call_tool("mcp_resources_list", {}))["items"]
        assert item["uriTemplate"] == "mcp-prompt:///topic"
        result = tool_data(
            await client.call_tool("mcp_resources_read", {"uri": item["uriTemplate"]})
        )
        assert result["messages"][0]["content"]["text"] == "Review Python"

    async with resource_client(
        registry, query="prompts=resource", version=version, answers={"topic": {"name": "Python"}}
    ) as (client, _):
        uri = "mcp-prompt:///topic"
        if pick(version).asks_in_arguments:
            with pytest.raises(ClientError, match="Use the resource tool"):
                await client.read_resource(uri)
        elif pick(version).can_ask:
            first = await client.request(
                Operation.READ_RESOURCE, ReadResourceParams(uri=uri), name=uri
            )
            assert first["resultType"] == "input_required"
            result = await client.request(
                Operation.READ_RESOURCE,
                ReadResourceParams(
                    uri=uri,
                    input_responses={
                        "topic": {"action": "accept", "content": {"name": "Python"}},
                    },
                ),
                name=uri,
            )
            assert json.loads(result["contents"][0]["text"])["messages"][0]["content"]["text"] == (
                "Review Python"
            )
        else:
            result = await client.read_resource(uri)
            assert json.loads(result.contents[0].text)["messages"][0]["content"]["text"] == (
                "Review Python"
            )


async def test_prompt_addresses_preserve_application_permissions():
    verifier = StaticVerifier({"token": Principal(subject="alice")})
    registry = Registry("prompts", "1", auth=Authorization(verifier, "https://example.test/mcp"))

    @registry.prompt
    async def private(args: Nothing, principal: Principal) -> str:
        if "private:read" not in principal.scopes:
            raise Rejected(Failure(FailureKind.ACCESS_DENIED, "private prompt"))
        return "private text"

    async with resource_client(
        registry,
        "resources=tool&resources=native&prompts=resource",
        headers={"Authorization": "Bearer token"},
    ) as (client, _):
        with pytest.raises(ClientError, match="private prompt"):
            await client.get_prompt("private", {})
        with pytest.raises(ClientError, match="private prompt"):
            await client.read_resource("mcp-prompt:///private")
        with pytest.raises(ClientError, match="private prompt"):
            await client.call_tool("mcp_resources_read", {"uri": "mcp-prompt:///private"})


@pytest.mark.parametrize("enabled", [False, True])
async def test_prompt_address_opt_in_and_reserved_scheme(enabled):
    registry = Registry("prompts", "1", resource_tools=enabled)

    async def real_resource(args: Nothing) -> str:
        return "ordinary data"

    uri = "mcp-prompt:///example"
    if enabled:
        with pytest.raises(ValueError, match="reserved scheme"):
            registry.resource(uri, real_resource)
    else:
        registry.resource(uri, real_resource)

    @registry.prompt
    async def example(args: Nothing) -> str:
        return "prompt text"

    query = "" if enabled else "prompts=resource"
    async with resource_client(registry, query=query) as (client, discovery):
        assert await client.list_resource_templates() == []
        if enabled:
            assert "resources" not in discovery["capabilities"]
            with pytest.raises(ClientError):
                await client.read_resource(uri)
        else:
            assert (await client.read_resource(uri)).contents[0].text == "ordinary data"


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
async def test_reserved_path_reads_through_both_interfaces(version: str):
    registry = resource_registry()
    template = "docker://containers/{name}/files/{+path}"

    @registry.resource(template)
    async def file(args: File) -> str:
        return f"{args.name}: {args.path}"

    async with resource_client(registry, "resources=tool&resources=native", version) as (
        client,
        _,
    ):
        assert template in {item.uri_template for item in await client.list_resource_templates()}
        for path, expected in (
            ("tmp/screenshots/frame.png", "tmp/screenshots/frame.png"),
            ("/tmp/screenshots/frame.png", "/tmp/screenshots/frame.png"),
            ("tmp%2Fscreenshots/caf%C3%A9%20+%2520.png", "tmp/screenshots/café +%20.png"),
            ("", ""),
        ):
            uri = f"docker://containers/cache/files/{path}"
            native = (await client.read_resource(uri)).contents[0]
            assert native.uri == uri
            assert native.text == f"cache: {expected}"
            tool = tool_data(await client.call_tool("mcp_resources_read", {"uri": uri}))
            assert tool["contents"][0]["text"] == native.text

        for uri in ("docker://containers/my/cache/files/frame.png", "file://entries/a/b"):
            with pytest.raises(ClientError):
                await client.read_resource(uri)
        encoded = await client.read_resource("file://entries/a%2Fb")
        assert encoded.contents[0].text == "entry a%2Fb"
        with pytest.raises(ClientError):
            await client.read_resource("docker://containers/cache/files/%FF")


@pytest.mark.parametrize("version", VERSIONS)
async def test_resource_tools_preserve_pagination_templates_and_contents(version: str):
    async with resource_client(resource_registry(page_size=1), version=version) as (
        client,
        discovery,
    ):
        first = tool_data(await client.call_tool("mcp_resources_list", {"templates": False}))
        assert [resource["uri"] for resource in first["items"]] == ["file://alpha"]
        second = tool_data(
            await client.call_tool(
                "mcp_resources_list", {"templates": False, "cursor": first["nextCursor"]}
            )
        )
        assert [resource["uri"] for resource in second["items"]] == ["file://binary"]
        assert "nextCursor" not in second
        templates = tool_data(await client.call_tool("mcp_resources_list", {"templates": True}))
        assert templates["items"][0]["uriTemplate"] == "file://entries/{name}"
        text = tool_data(
            await client.call_tool("mcp_resources_read", {"uri": "file://entries/report"})
        )
        assert text["contents"][0]["uri"] == "file://entries/report"
        assert text["contents"][0]["text"] == "entry report"
        binary = tool_data(await client.call_tool("mcp_resources_read", {"uri": "file://binary"}))
        assert binary["contents"][0]["uri"] == "file://binary"
        assert binary["contents"][0]["mimeType"] == "application/octet-stream"
        assert binary["contents"][0]["blob"] == "AP8="


@pytest.mark.parametrize("version", VERSIONS)
@pytest.mark.parametrize(
    "arguments", [{}, {"templates": None}, {"templates": True}, {"templates": False}]
)
@pytest.mark.parametrize("page_size", [1, 3, None])
async def test_resource_list_filter_pages_selected_catalogues(
    version: str, arguments: dict[str, Any], page_size: int | None
):
    registry = resource_registry(page_size=page_size)

    @registry.resource("file://reports/{name}")
    async def report(args: Entry) -> str:
        raise AssertionError("listing must not read resources")

    async with resource_client(registry, version=version) as (client, discovery):
        expected = []
        if arguments.get("templates") is not True:
            expected.extend(["file://alpha", "file://binary"])
        if arguments.get("templates") is not False:
            expected.extend(["file://entries/{name}", "file://reports/{name}"])
        collected = []
        cursor = None
        seen = set()
        while True:
            page_arguments = {**arguments, "cursor": cursor} if cursor else arguments
            listed = tool_data(await client.call_tool("mcp_resources_list", page_arguments))
            assert set(listed) - {"nextCursor"} == {"items"}
            assert len(listed["items"]) <= (page_size or 4)
            collected.extend(item.get("uri", item.get("uriTemplate")) for item in listed["items"])
            cursor = listed.get("nextCursor")
            if cursor is None:
                break
            assert cursor not in seen
            seen.add(cursor)
        assert collected == expected


@pytest.mark.parametrize(
    "arguments", [{}, {"templates": None}, {"templates": True}, {"templates": False}]
)
async def test_resource_list_filter_preserves_empty_selected_catalogues(arguments: dict[str, Any]):
    async with resource_client(Registry("empty", "1")) as (client, discovery):
        listed = tool_data(await client.call_tool("mcp_resources_list", arguments))
        assert listed == {"items": []}


@pytest.mark.parametrize("version", VERSIONS)
@pytest.mark.parametrize("templates", [None, True, False])
@pytest.mark.parametrize(
    "query,resources,resource_templates",
    [
        (None, ["file://alpha", "file://binary"], ["file://entries/{name}"]),
        ("", ["file://alpha", "file://binary"], ["file://entries/{name}"]),
        ("pha", ["file://alpha"], []),
        ("entries/{n", [], ["file://entries/{name}"]),
        ("FILE://", ["file://alpha", "file://binary"], ["file://entries/{name}"]),
        ("AlPhA", ["file://alpha"], []),
        ("ENTRIES", [], ["file://entries/{name}"]),
        ("file://.*", [], []),
        ("missing://", [], []),
    ],
)
async def test_resource_list_query_matches_literal_uri_substring(
    version: str,
    templates: bool | None,
    query: str | None,
    resources: list[str],
    resource_templates: list[str],
):
    async with resource_client(resource_registry(), version=version) as (client, discovery):
        listed = tool_data(
            await client.call_tool("mcp_resources_list", {"templates": templates, "query": query})
        )
        expected_resources = resources if templates is not True else []
        expected_templates = resource_templates if templates is not False else []
        assert [item["uri"] for item in listed["items"] if "uri" in item] == expected_resources
        assert [
            item["uriTemplate"] for item in listed["items"] if "uriTemplate" in item
        ] == expected_templates
        assert set(listed) == {"items"}


@pytest.mark.parametrize("version", VERSIONS)
async def test_resource_list_query_casefold_preserves_original_uris(version: str):
    registry = resource_registry()

    @registry.resource("file://Straße")
    async def street(args: Nothing) -> str:
        return "street"

    @registry.resource("file://Straße/{name}")
    async def address(args: Entry) -> str:
        return args.name

    async with resource_client(registry, version=version) as (client, discovery):
        listed = tool_data(await client.call_tool("mcp_resources_list", {"query": "STRASSE"}))
        assert len(listed["items"]) == 2
        assert listed["items"][0]["uri"] == "file://Straße"
        assert listed["items"][1]["uriTemplate"] == "file://Straße/{name}"


@pytest.mark.parametrize("version", VERSIONS)
async def test_resource_list_query_filters_before_pagination(version: str):
    registry = resource_registry(page_size=1)

    @registry.resource("file://entries/index")
    async def index(args: Nothing) -> str:
        raise AssertionError("listing must not read resources")

    async with resource_client(registry, version=version) as (client, discovery):
        arguments = {"query": "EnTrIeS"}
        first = tool_data(await client.call_tool("mcp_resources_list", arguments))
        assert [item["uri"] for item in first["items"]] == ["file://entries/index"]
        second = tool_data(
            await client.call_tool(
                "mcp_resources_list", {**arguments, "cursor": first["nextCursor"]}
            )
        )
        assert [item["uriTemplate"] for item in second["items"]] == ["file://entries/{name}"]
        assert "nextCursor" not in second
        rejected = await client.call_tool(
            "mcp_resources_list", {"query": "file://alpha", "cursor": first["nextCursor"]}
        )
        assert isinstance(rejected, CallToolResult)
        assert rejected.is_error


@pytest.mark.parametrize("version", VERSIONS)
async def test_resource_list_schema_exposes_nullable_filters_and_cursor(version: str):
    async with resource_client(resource_registry(), version=version) as (client, discovery):
        tools = {tool.name: tool for tool in await client.list_tools()}
        schema = tools["mcp_resources_list"].input_schema
        reserved = {"mcpAnswers", "mcpState"} if version in {"2024-11-05", "2025-03-26"} else set()
        assert set(schema["properties"]) == {"templates", "query", "cursor"} | reserved
        assert schema["additionalProperties"] is False
        assert not schema.get("required")
        templates = schema["properties"]["templates"]
        assert templates["default"] is None
        types = templates.get("type") or [item["type"] for item in templates["anyOf"]]
        assert set(types) == {"boolean", "null"}
        query = schema["properties"]["query"]
        assert query["default"] is None
        types = query.get("type") or [item["type"] for item in query["anyOf"]]
        assert set(types) == {"string", "null"}


@pytest.mark.parametrize("version", VERSIONS)
@pytest.mark.parametrize("authorized", [False, True])
async def test_resource_lists_preserve_native_scope_and_revision_projection(
    version: str, authorized: bool
):
    registry, calls = registry_with_resources({"files:read", "profile:read"} if authorized else ())
    async with resource_client(
        registry,
        query="resources=tool&resources=native",
        version=version,
        headers={"Authorization": "Bearer test-token"},
    ) as (client, discovery):
        resources = await client.request(Operation.LIST_RESOURCES, ListParams())
        templates = await client.request(Operation.LIST_RESOURCE_TEMPLATES, ListParams())
        listed = tool_data(await client.call_tool("mcp_resources_list", {}))
        native_items = resources["resources"] + templates["resourceTemplates"]
        assert listed == {
            "items": [
                {key: value for key, value in item.items() if key not in {"name", "title"}}
                for item in native_items
            ]
        }
        assert listed["items"][0]["uri"] == "files://config"
        assert listed["items"][1]["uriTemplate"] == "files://entries/{id}"
    assert calls == []


@pytest.mark.parametrize("templates", [False, True])
async def test_resource_tool_items_omit_labels_and_preserve_native_metadata(templates: bool):
    registry = Registry("metadata", "1")

    @registry.resource(
        "file://index",
        name="index-resource",
        title="Index title",
        description="Index description",
        mime_type="application/json",
    )
    async def index(args: Nothing) -> str:
        raise AssertionError("listing must not read resources")

    @registry.resource(
        "file://entries/{name}",
        name="entry-resource",
        title="Entry title",
        description="Entry description",
        mime_type="text/plain",
    )
    async def entry(args: Entry) -> str:
        raise AssertionError("listing must not read resources")

    async with resource_client(registry, query="resources=tool&resources=native") as (
        client,
        discovery,
    ):
        operation = Operation.LIST_RESOURCE_TEMPLATES if templates else Operation.LIST_RESOURCES
        native_key = "resourceTemplates" if templates else "resources"
        before = await client.request(operation, ListParams())
        native_item = before[native_key][0]
        assert native_item["name"] == ("entry-resource" if templates else "index-resource")
        assert native_item["title"] == ("Entry title" if templates else "Index title")
        listed = tool_data(await client.call_tool("mcp_resources_list", {"templates": templates}))
        expected = (
            {
                "uriTemplate": "file://entries/{name}",
                "description": "Entry description",
                "mimeType": "text/plain",
            }
            if templates
            else {
                "uri": "file://index",
                "description": "Index description",
                "mimeType": "application/json",
            }
        )
        assert listed == {"items": [expected]}
        assert await client.request(operation, ListParams()) == before


@pytest.mark.parametrize("templates", [False, True])
async def test_resource_list_cursor_cannot_skip_into_a_different_kind(templates: bool):
    registry = resource_registry(page_size=1)

    @registry.resource("file://reports/{name}")
    async def report(args: Entry) -> str:
        return args.name

    async with resource_client(registry) as (client, discovery):
        first = tool_data(await client.call_tool("mcp_resources_list", {"templates": templates}))
        result = await client.call_tool(
            "mcp_resources_list", {"templates": not templates, "cursor": first["nextCursor"]}
        )
        assert isinstance(result, CallToolResult)
        assert result.is_error
        assert isinstance(result.content[0], TextContent)
        assert "cursor" in result.content[0].text


@pytest.mark.parametrize(
    "name,arguments",
    [
        ("mcp_resources_read", {}),
        ("mcp_resources_read", {"uri": "file://missing"}),
        ("mcp_resources_list", {"templates": "unknown"}),
        ("mcp_resources_list", {"templates": []}),
        ("mcp_resources_list", {"kind": "resources"}),
        ("mcp_resources_list", {"query": 123}),
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


async def test_legacy_sse_carries_prompt_resource_mode_to_the_post_url():
    registry = Registry("prompts", "1")

    @registry.prompt
    async def example(args: Nothing) -> str:
        return "prompt text"

    app = SseEndpoint(registry).setup(web.Application())
    async with TestServer(app) as server:
        client = OldClient(str(server.make_url("")).rstrip("/"))
        try:
            endpoint = await open_legacy_resources(client, "prompts=resource")
            assert parse_qs(urlsplit(endpoint).query)["prompts"] == ["resource"]
            discovery = await client.call(
                "initialize",
                {
                    "protocolVersion": "2024-11-05",
                    "clientInfo": {"name": "test", "version": "1"},
                    "capabilities": {},
                },
            )
            assert "resources" in discovery["result"]["capabilities"]
            listed = await client.call("resources/templates/list", {}, request_id=2)
            (template,) = listed["result"]["resourceTemplates"]
            read = await client.call(
                "resources/read", {"uri": template["uriTemplate"]}, request_id=3
            )
            result = json.loads(read["result"]["contents"][0]["text"])
            assert result["messages"][0]["content"]["text"] == "prompt text"
        finally:
            await client.close()


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


@pytest.mark.parametrize("version", VERSIONS)
async def test_the_resource_catalogue_carries_no_icon(version: str):
    """An icon is a URL or bytes for a person to look at, and this catalogue is read by
    a model. The name and the title are left out for the same reason.
    """
    registry = Registry("icons", "1")
    icon = Icon(src="https://example.test/i.png")
    registry.resource("file://alpha", described_resource, icons=[icon], title="Alpha")
    registry.resource("file://entries/{name}", entry_resource, name="entry", icons=[icon])

    async with resource_client(registry, version=version) as (client, discovery):
        for templates in (False, True):
            listed = tool_data(
                await client.call_tool("mcp_resources_list", {"templates": templates})
            )
            assert listed["items"], listed
            for item in listed["items"]:
                assert "icons" not in item
                assert "title" not in item
                assert "name" not in item
