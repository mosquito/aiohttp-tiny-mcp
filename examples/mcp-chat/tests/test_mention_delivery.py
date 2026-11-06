from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Mapping
from contextlib import AsyncExitStack, asynccontextmanager, suppress
from pathlib import Path
from typing import Any

import aiohttp
import pytest
from aiohttp.test_utils import BaseTestServer, TestServer
from aiohttp_tiny_mcp import AdapterSet, CallToolResult, Client
from aiohttp_tiny_mcp.protocol.adapter import Adapter
from aiohttp_tiny_mcp.protocol.models import TextResourceContents

from mcp_chat.auth import issue_token
from mcp_chat.notifications import ACTOR, RECIPIENTS, NotificationHub
from mcp_chat.server import create_app

ADAPTERS = [AdapterSet.default().by_version[version] for version in ("2026-07-28", "2025-11-25")]
SECRET = "mention-delivery-test-secret-at-least-thirty-two-bytes"


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


async def connect(
    stack: AsyncExitStack,
    server: BaseTestServer,
    adapter: Adapter,
    user: str,
    org: str = "example.com",
) -> Client:
    token = issue_token(SECRET, org, user)
    session = await stack.enter_async_context(
        aiohttp.ClientSession(headers={"Authorization": f"Bearer {token}"})
    )
    client = await stack.enter_async_context(
        Client(str(server.make_url("/mcp?org=spoofed&user=spoofed")), adapter, session=session)
    )
    await client.initialize()
    return client


@asynccontextmanager
async def listen(
    client: Client, monkeypatch: pytest.MonkeyPatch
) -> AsyncGenerator[asyncio.Queue[Mapping[str, Any]]]:
    identity = await call(client, "identity")
    actor = (identity["org"], identity["user"])
    ready = asyncio.Event()
    position = NotificationHub.position

    async def positioned(hub: NotificationHub, where: str) -> str:
        cursor = await position(hub, where)
        if ACTOR.get() == actor:
            ready.set()
        return cursor

    monkeypatch.setattr(NotificationHub, "position", positioned)
    queue: asyncio.Queue[Mapping[str, Any]] = asyncio.Queue()

    async def receive() -> None:
        async for event in client.listen(resources=["chat://inbox", "chat://channels/marker"]):
            assert RECIPIENTS not in event
            await queue.put(event)

    task = asyncio.create_task(receive())
    try:
        await asyncio.wait_for(ready.wait(), 3)
        yield queue
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


async def next_uri(queue: asyncio.Queue[Mapping[str, Any]]) -> str:
    event = await asyncio.wait_for(queue.get(), 3)
    assert event["method"] == "notifications/resources/updated"
    return event["params"]["uri"]


@pytest.mark.parametrize("adapter", ADAPTERS, ids=["modern", "legacy"])
async def test_public_mentions_are_paged_and_acknowledged_without_membership(
    tmp_path: Path, adapter: Adapter
) -> None:
    async with AsyncExitStack() as stack:
        server = await stack.enter_async_context(
            TestServer(create_app(tmp_path / "chat.sqlite3", jwt_secret=SECRET))
        )
        alice = await connect(stack, server, adapter, "alice")
        bob = await connect(stack, server, adapter, "bob")
        discovery = await bob.initialize()
        rules = (await bob.read_resource("chat://rules")).contents[0]
        assert isinstance(rules, TextResourceContents)
        assert discovery["instructions"] == rules.text
        assert "case-sensitive" in rules.text and "1–64 ASCII" in rules.text
        assert "@alice@example.com" in rules.text
        assert "Without membership, use mark_read" in rules.text
        await call(alice, "create_channel", name="news")
        plain = await call(alice, "post_message", channel="news", text="Public history only")
        root = await call(alice, "post_message", channel="news", text="@bob, review this @bob")
        reply = await call(
            alice, "post_message", channel="news", text="@bob, follow-up", parent_id=root["id"]
        )
        assert (await call(bob, "list_subscriptions"))["subscriptions"] == []
        page = await call(bob, "inbox", limit=1)
        assert [message["id"] for message in page["messages"]] == [root["id"]]
        page = await call(bob, "inbox", after=page["next_after"], limit=1)
        assert [message["id"] for message in page["messages"]] == [reply["id"]]
        assert (await call(bob, "inbox", after=page["next_after"]))["messages"] == []
        await refused(bob, "mark_read", channel="news", message_id=plain["id"])
        await call(bob, "mark_read", channel="news", message_id=root["id"])
        await call(bob, "mark_read", channel="news", message_id=root["id"])
        assert [message["id"] for message in (await call(bob, "inbox"))["messages"]] == [
            reply["id"]
        ]
        await call(bob, "subscribe", channel="news")
        both = await call(alice, "post_message", channel="news", text="@bob @bob joined")
        await call(bob, "post_message", channel="news", text="@bob own message")
        assert [message["id"] for message in (await call(bob, "inbox"))["messages"]] == [
            reply["id"],
            both["id"],
        ]
        await call(bob, "mark_read", channel="news", message_id=both["id"])
        await call(bob, "unsubscribe", channel="news")
        assert (await call(bob, "inbox"))["messages"] == []
        late = await call(alice, "post_message", channel="news", text="@bob after leaving")
        assert [message["id"] for message in (await call(bob, "inbox"))["messages"]] == [late["id"]]
        await call(bob, "mark_read", channel="news", message_id=late["id"])
        assert (await call(bob, "inbox"))["messages"] == []
        assert (await call(bob, "list_subscriptions"))["subscriptions"] == []


@pytest.mark.parametrize("adapter", ADAPTERS, ids=["modern", "legacy"])
async def test_mentions_do_not_grant_dm_or_cross_organization_access(
    tmp_path: Path, adapter: Adapter
) -> None:
    async with AsyncExitStack() as stack:
        server = await stack.enter_async_context(
            TestServer(create_app(tmp_path / "chat.sqlite3", jwt_secret=SECRET))
        )
        alice = await connect(stack, server, adapter, "alice")
        bob = await connect(stack, server, adapter, "bob")
        foreign = await connect(stack, server, adapter, "alice", "other.example")
        private = await call(alice, "send_dm", user="charlie", text="@bob confidential")
        await call(foreign, "create_channel", name="news")
        outside = await call(foreign, "post_message", channel="news", text="@bob foreign")
        assert (await call(bob, "inbox"))["messages"] == []
        assert (await call(bob, "list_dms"))["conversations"] == []
        assert (await call(bob, "search", query="bob"))["messages"] == []
        for message in (private, outside):
            await refused(bob, "history", channel=message["channel"])
            await refused(bob, "thread", message_id=message["id"])
            await refused(bob, "mark_read", channel=message["channel"], message_id=message["id"])


@pytest.mark.parametrize("adapter", ADAPTERS, ids=["modern", "legacy"])
async def test_mention_notifications_target_only_the_current_public_recipients(
    tmp_path: Path, adapter: Adapter, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with AsyncExitStack() as stack:
        server = await stack.enter_async_context(
            TestServer(create_app(tmp_path / "chat.sqlite3", jwt_secret=SECRET))
        )
        alice = await connect(stack, server, adapter, "alice")
        bob = await connect(stack, server, adapter, "bob")
        outsider = await connect(stack, server, adapter, "outsider")
        foreign = await connect(stack, server, adapter, "bob", "other.example")
        await call(alice, "create_channel", name="news")
        await call(alice, "create_channel", name="marker")
        await call(foreign, "create_channel", name="marker")
        bob_events = await stack.enter_async_context(listen(bob, monkeypatch))
        outsider_events = await stack.enter_async_context(listen(outsider, monkeypatch))
        foreign_events = await stack.enter_async_context(listen(foreign, monkeypatch))
        root = await call(alice, "post_message", channel="news", text="@bob, ping @bob")
        assert await next_uri(bob_events) == "chat://inbox"
        await call(alice, "post_message", channel="news", text="No mention")
        await call(alice, "send_dm", user="charlie", text="@bob private mention")
        await call(alice, "post_message", channel="marker", text="Public sentinel")
        assert await next_uri(bob_events) == "chat://channels/marker"
        assert await next_uri(outsider_events) == "chat://channels/marker"
        await call(foreign, "post_message", channel="marker", text="Foreign sentinel")
        assert await next_uri(foreign_events) == "chat://channels/marker"
        await call(alice, "post_message", channel="news", text="@bob reply", parent_id=root["id"])
        assert await next_uri(bob_events) == "chat://inbox"
        assert len((await call(bob, "inbox"))["messages"]) == 2
