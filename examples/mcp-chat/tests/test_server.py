from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import aiohttp
import pytest
from aiohttp.test_utils import BaseTestServer, TestServer
from aiohttp_tiny_mcp import Client, ClientError
from aiohttp_tiny_mcp.protocol.models import CallToolResult, TextResourceContents
from aiohttp_tiny_mcp.protocol.selection import AdapterSet

from mcp_chat.server import create_app

MODERN = AdapterSet.default().by_version["2026-07-28"]
LEGACY = AdapterSet.default().by_version["2025-11-25"]


@pytest.fixture
async def server(tmp_path: Path) -> AsyncIterator[BaseTestServer]:
    async with TestServer(create_app(tmp_path / "chat.sqlite3")) as running:
        yield running


def endpoint(server: BaseTestServer, org: str = "example.com", user: str = "alice") -> str:
    return str(server.make_url(f"/mcp?org={org}&user={user}"))


async def call(client: Client, tool: str, **arguments: Any) -> dict[str, Any]:
    result = await client.call_tool(tool, arguments)
    assert isinstance(result, CallToolResult)
    assert not result.is_error, result
    assert isinstance(result.structured_content, dict)
    return result.structured_content


async def refused(client: Client, tool: str, **arguments: Any) -> None:
    result = await client.call_tool(tool, arguments)
    assert isinstance(result, CallToolResult)
    assert result.is_error


@pytest.mark.parametrize("adapter", [MODERN, LEGACY], ids=["modern", "legacy"])
async def test_chat_workflow(server: BaseTestServer, adapter: Any) -> None:
    async with (
        Client(endpoint(server), adapter) as alice,
        Client(endpoint(server, user="bob"), adapter) as bob,
    ):
        assert await call(alice, "identity") == {"org": "example.com", "user": "alice"}
        await call(alice, "create_channel", name="team", description="Team news")
        assert len((await call(alice, "list_subscriptions"))["subscriptions"]) == 1
        old = await call(alice, "post_message", channel="team", text="Before joining")
        await call(bob, "subscribe", channel="team")
        assert (await call(bob, "inbox"))["messages"] == []

        first = await call(alice, "post_message", channel="team", text="First unread")
        reply = await call(bob, "post_message", channel="team", text="Reply", parent_id=first["id"])
        second = await call(alice, "post_message", channel="team", text="Second unread")
        await call(bob, "subscribe", channel="team")
        page = await call(bob, "inbox", limit=1)
        assert [row["id"] for row in page["messages"]] == [first["id"]]
        page = await call(bob, "inbox", after=page["next_after"], limit=1)
        assert [row["id"] for row in page["messages"]] == [second["id"]]
        assert len((await call(bob, "inbox"))["messages"]) == 2

        thread = await call(alice, "thread", message_id=first["id"])
        assert [row["id"] for row in thread["messages"]] == [first["id"], reply["id"]]
        await call(bob, "mark_read", channel="team", message_id=second["id"])
        await call(bob, "mark_read", channel="team", message_id=first["id"])
        assert (await call(bob, "inbox"))["messages"] == []
        assert [row["id"] for row in (await call(alice, "inbox"))["messages"]] == [reply["id"]]

        history = await call(bob, "history", channel="team", after=old["id"], limit=2)
        assert [row["id"] for row in history["messages"]] == [first["id"], reply["id"]]
        search = await call(bob, "search", query="Second", channel="team")
        assert [row["id"] for row in search["messages"]] == [second["id"]]
        await call(bob, "unsubscribe", channel="team")
        await call(alice, "post_message", channel="team", text="After leaving")
        assert (await call(bob, "inbox"))["messages"] == []
        assert (await call(bob, "list_subscriptions"))["subscriptions"] == []
        await refused(bob, "post_message", channel="team", text="Cannot post")


async def test_organizations_isolate_all_data(server: BaseTestServer) -> None:
    async with (
        Client(endpoint(server), MODERN) as first,
        Client(endpoint(server, org="other.com"), MODERN) as other,
    ):
        await call(first, "create_channel", name="team")
        message = await call(first, "post_message", channel="team", text="Private to org")
        assert {row["name"] for row in (await call(other, "list_channels"))["channels"]} == {
            "general",
            "random",
            "tasks",
        }
        assert (await call(other, "list_subscriptions"))["subscriptions"] == []
        assert (await call(other, "inbox"))["messages"] == []
        assert (await call(other, "search", query="Private"))["messages"] == []
        await refused(other, "history", channel="team")
        await refused(other, "subscribe", channel="team")
        await call(other, "create_channel", name="team")
        assert (await call(other, "history", channel="team"))["messages"] == []
        await refused(other, "thread", message_id=message["id"])
        await refused(other, "mark_read", channel="team", message_id=message["id"])
        await refused(
            other, "post_message", channel="team", text="Foreign parent", parent_id=message["id"]
        )
        own = await call(other, "post_message", channel="team", text="Other organization")
        assert own["id"] != message["id"]
        history = await call(first, "history", channel="team")
        assert [row["id"] for row in history["messages"]] == [message["id"]]
        resources = await other.read_resource("chat://channels/team")
        assert "Private to org" not in str(resources)
        assert "Other organization" in str(resources)


@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("identity", {"org": "other.com"}),
        ("create_channel", {"name": "../secret"}),
        ("create_channel", {"name": ""}),
        ("create_channel", {"name": "a" * 65}),
        ("post_message", {"channel": "team", "text": "   "}),
        ("post_message", {"channel": "team", "text": "a" * 16001}),
        ("post_message", {"channel": "team", "text": "hello", "user": "bob"}),
        ("history", {"channel": "team", "after": -1}),
        ("history", {"channel": "team", "limit": 0}),
        ("inbox", {"limit": 101}),
        ("search", {"query": ""}),
    ],
)
async def test_invalid_arguments(
    server: BaseTestServer, name: str, arguments: dict[str, Any]
) -> None:
    async with Client(endpoint(server), MODERN) as client:
        await call(client, "create_channel", name="team")
        await refused(client, name, **arguments)


@pytest.mark.parametrize(
    "query",
    ["", "?org=", "?org=a&org=b", "?org=example.com&user=", "?org=example.com&user=a&user=b"],
)
async def test_invalid_context_is_http_error(server: BaseTestServer, query: str) -> None:
    async with aiohttp.ClientSession() as session:
        async with session.post(
            str(server.make_url(f"/mcp{query}")),
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            headers={"Accept": "application/json, text/event-stream"},
        ) as response:
            assert response.status == 400


async def test_default_user(server: BaseTestServer) -> None:
    async with Client(str(server.make_url("/mcp?org=example.com")), MODERN) as client:
        assert await call(client, "identity") == {"org": "example.com", "user": "human"}


async def test_legacy_session_cannot_change_org_or_user(server: BaseTestServer) -> None:
    async with Client(endpoint(server), LEGACY) as client:
        await client.initialize()
        assert client.session_id is not None
        assert client.session is not None
        for org, user in [("other.com", "alice"), ("example.com", "bob")]:
            async with client.session.post(
                endpoint(server, org=org, user=user),
                json={"jsonrpc": "2.0", "id": 42, "method": "tools/list", "params": {}},
                headers={
                    "MCP-Protocol-Version": LEGACY.version,
                    "Mcp-Session-Id": client.session_id,
                    "Accept": "application/json, text/event-stream",
                },
            ) as response:
                assert response.status in {403, 404}
        assert await call(client, "identity") == {"org": "example.com", "user": "alice"}


async def event(response: aiohttp.ClientResponse) -> dict[str, Any]:
    while line := await asyncio.wait_for(response.content.readline(), 3):
        if line.startswith(b"data:"):
            return json.loads(line[5:])
    pytest.fail("Notification stream closed before an event.")


async def test_notifications_stay_in_organization(server: BaseTestServer) -> None:
    async with (
        Client(endpoint(server), MODERN) as local,
        Client(endpoint(server, org="other.com"), MODERN) as other,
        aiohttp.ClientSession() as session,
    ):
        await call(local, "create_channel", name="team")
        await call(other, "create_channel", name="foreign")
        async with session.post(
            endpoint(server),
            headers={
                "MCP-Protocol-Version": MODERN.version,
                "Mcp-Method": "subscriptions/listen",
                "Accept": "text/event-stream",
            },
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "subscriptions/listen",
                "params": {
                    "notifications": {
                        "resourceSubscriptions": [
                            "chat://channels/team",
                            "chat://channels/foreign",
                        ]
                    },
                    "_meta": {
                        "io.modelcontextprotocol/protocolVersion": MODERN.version,
                        "io.modelcontextprotocol/clientCapabilities": {},
                    },
                },
            },
        ) as response:
            assert response.status == 200
            assert (await event(response))["method"] == "notifications/subscriptions/acknowledged"
            await call(other, "post_message", channel="foreign", text="Other org event")
            await call(local, "post_message", channel="team", text="Local event")
            update = await event(response)
            assert update["method"] == "notifications/resources/updated"
            assert update["params"]["uri"] == "chat://channels/team"


async def test_data_survives_server_restart(tmp_path: Path) -> None:
    path = tmp_path / "chat.sqlite3"
    async with TestServer(create_app(path)) as first:
        async with (
            Client(endpoint(first), MODERN) as alice,
            Client(endpoint(first, user="bob"), MODERN) as bob,
        ):
            await call(alice, "create_channel", name="team")
            await call(bob, "subscribe", channel="team")
            seen = await call(alice, "post_message", channel="team", text="Read this")
            await call(bob, "mark_read", channel="team", message_id=seen["id"])
            unread = await call(alice, "post_message", channel="team", text="Keep unread")
    async with TestServer(create_app(path)) as second:
        async with Client(endpoint(second, user="bob"), MODERN) as bob:
            assert len((await call(bob, "list_subscriptions"))["subscriptions"]) == 1
            assert [row["id"] for row in (await call(bob, "inbox"))["messages"]] == [unread["id"]]
            assert len((await call(bob, "history", channel="team"))["messages"]) == 2


async def read_json(client: Client, uri: str) -> dict[str, Any]:
    resource = await client.read_resource(uri)
    content = resource.contents[0]
    assert isinstance(content, TextResourceContents)
    assert content.mime_type == "application/json"
    return json.loads(content.text)


@pytest.mark.parametrize("query", ['"unclosed', "org:example", "NEAR(release plan, nope)"])
async def test_fts_syntax_errors_are_clean_tool_results(server: BaseTestServer, query: str) -> None:
    async with Client(endpoint(server), MODERN) as client:
        result = await client.call_tool("search", {"query": query})
        assert isinstance(result, CallToolResult)
        assert result.is_error
        assert "Invalid full-text search query" in str(result.content)
        assert "OperationalError" not in str(result.content)
        assert (await call(client, "search", query="release"))["messages"] == []


@pytest.mark.parametrize("adapter", [MODERN, LEGACY], ids=["modern", "legacy"])
@pytest.mark.parametrize("uri", ["chat://channels", "chat://channels/general", "chat://inbox"])
async def test_first_resource_read_bootstraps_org(
    server: BaseTestServer, adapter: Any, uri: str
) -> None:
    async with Client(endpoint(server, org="fresh.example"), adapter) as client:
        await read_json(client, uri)
        channels = (await read_json(client, "chat://channels"))["channels"]
        assert [row["name"] for row in channels] == ["general", "random", "tasks"]
        assert not any(row["subscribed"] for row in channels)
        assert {row["org"] for row in channels} == {"fresh.example"}
        await call(client, "subscribe", channel="general")
        await call(client, "unsubscribe", channel="general")
        assert (await read_json(client, "chat://subscriptions"))["subscriptions"] == []
        await refused(client, "post_message", channel="general", text="Must explicitly join")


@pytest.mark.parametrize("adapter", [MODERN, LEGACY], ids=["modern", "legacy"])
async def test_chat_resources_and_rules(server: BaseTestServer, adapter: Any) -> None:
    async with (
        Client(endpoint(server), adapter) as alice,
        Client(endpoint(server, user="bob"), adapter) as bob,
        Client(endpoint(server, org="other.example"), adapter) as other,
    ):
        listed = {resource.uri for resource in await alice.list_resources()}
        assert listed >= {"chat://channels", "chat://inbox", "chat://subscriptions", "chat://rules"}
        await call(alice, "subscribe", channel="general")
        await call(bob, "subscribe", channel="general")
        root = await call(alice, "post_message", channel="general", text="Root message")
        reply = await call(
            alice, "post_message", channel="general", text="Reply", parent_id=root["id"]
        )
        channel = await read_json(bob, "chat://channels/general")
        assert [row["id"] for row in channel["messages"]] == [root["id"], reply["id"]]
        page = await read_json(bob, f"chat://channels/general/messages/{root['id']}")
        assert [row["id"] for row in page["messages"]] == [reply["id"]]
        thread = await read_json(bob, f"chat://threads/{root['id']}")
        assert thread == channel
        thread_page = await read_json(bob, f"chat://threads/{root['id']}/messages/{root['id']}")
        assert thread_page == page
        assert (await read_json(bob, "chat://inbox"))["messages"] == channel["messages"]
        assert len((await call(bob, "inbox"))["messages"]) == 2
        assert (await read_json(other, "chat://channels/general"))["messages"] == []
        assert (await read_json(other, "chat://inbox"))["messages"] == []
        with pytest.raises(ClientError):
            await other.read_resource(f"chat://threads/{root['id']}")
        rules = (await bob.read_resource("chat://rules")).contents[0]
        assert isinstance(rules, TextResourceContents)
        assert rules.mime_type == "text/markdown"
        discovery = await bob.initialize()
        assert discovery["instructions"] == rules.text
        for word in ["general", "random", "tasks", "parent_id", "mark_read", "secrets"]:
            assert word in rules.text
        tools = await bob.list_tools()
        assert len(tools) == 15
        for tool in tools:
            assert f"{tool.name}(" in tool.description
            assert tool.description in rules.text
