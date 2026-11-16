from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Mapping, Sequence
from contextlib import AsyncExitStack, asynccontextmanager, suppress
from pathlib import Path
from typing import Any

import aiohttp
import pytest
from aiohttp.test_utils import BaseTestServer, TestServer
from aiohttp_tiny_mcp import AdapterSet, CallToolResult, Client
from aiohttp_tiny_mcp.protocol.adapter import Adapter
from aiohttp_tiny_mcp.storage.hub import START, Event, MemoryHub

from mcp_chat.auth import issue_token
from mcp_chat.notifications import ACTOR, RECIPIENTS, NotificationHub
from mcp_chat.server import create_app

ADAPTERS = [AdapterSet.default().by_version[version] for version in ("2026-07-28", "2025-11-25")]
SECRET = "notification-test-signing-secret-for-private-recipient-isolation"


async def call(client: Client, tool: str, **arguments: Any) -> dict[str, Any]:
    result = await client.call_tool(tool, arguments)
    assert isinstance(result, CallToolResult)
    assert not result.is_error, result
    assert isinstance(result.structured_content, dict)
    return result.structured_content


async def client_for(
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
        Client(
            str(server.make_url("/mcp?org=spoofed.example&user=spoofed")), adapter, session=session
        )
    )
    await client.initialize()
    return client


@asynccontextmanager
async def listening(
    client: Client, resources: Sequence[str], monkeypatch: pytest.MonkeyPatch
) -> AsyncGenerator[asyncio.Queue[Mapping[str, Any]], None]:
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
        async for event in client.listen(resources=resources):
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
    assert RECIPIENTS not in event
    return event["params"]["uri"]


@pytest.mark.parametrize("adapter", ADAPTERS, ids=["modern", "legacy"])
async def test_membership_messages_threads_and_read_positions_notify(
    tmp_path: Path, adapter: Adapter, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with AsyncExitStack() as stack:
        server = await stack.enter_async_context(
            TestServer(create_app(tmp_path / "chat.sqlite3", jwt_secret=SECRET))
        )
        alice = await client_for(stack, server, adapter, "alice")
        bob = await client_for(stack, server, adapter, "bob")
        alice_watch = await client_for(stack, server, adapter, "alice")
        bob_watch = await client_for(stack, server, adapter, "bob")
        alice_events = await stack.enter_async_context(
            listening(alice_watch, ["chat://subscriptions"], monkeypatch)
        )
        bob_events = await stack.enter_async_context(
            listening(bob_watch, ["chat://inbox"], monkeypatch)
        )
        await call(alice, "create_channel", name="news")
        assert await next_uri(alice_events) == "chat://subscriptions"
        await call(bob, "subscribe", channel="news")
        assert await next_uri(bob_events) == "chat://inbox"
        root = await call(alice, "post_message", channel="news", text="Release announcement")
        assert await next_uri(bob_events) == "chat://inbox"
        await call(bob, "mark_read", channel="news", message_id=root["id"])
        assert await next_uri(bob_events) == "chat://inbox"
        assert (await call(bob, "inbox"))["messages"] == []
        thread_watch = await client_for(stack, server, adapter, "bob")
        thread_events = await stack.enter_async_context(
            listening(thread_watch, [f"chat://threads/{root['id']}"], monkeypatch)
        )
        await call(
            alice,
            "post_message",
            channel="news",
            text="More detail",
            parent_id=root["id"],
            send_to_channel=True,
        )
        assert await next_uri(thread_events) == f"chat://threads/{root['id']}"
        assert await next_uri(bob_events) == "chat://inbox"
        await call(bob, "unsubscribe", channel="news")
        assert await next_uri(bob_events) == "chat://inbox"
        assert (await call(bob, "inbox"))["messages"] == []


@pytest.mark.parametrize("adapter", ADAPTERS, ids=["modern", "legacy"])
async def test_dm_notifications_do_not_leak_to_guessed_uris_or_other_organizations(
    tmp_path: Path, adapter: Adapter, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with AsyncExitStack() as stack:
        server = await stack.enter_async_context(
            TestServer(create_app(tmp_path / "chat.sqlite3", jwt_secret=SECRET))
        )
        alice = await client_for(stack, server, adapter, "alice")
        bob = await client_for(stack, server, adapter, "bob")
        charlie = await client_for(stack, server, adapter, "charlie")
        foreign = await client_for(stack, server, adapter, "bob", "other.example")
        conversation = await call(alice, "open_dm", user="bob")
        root = await call(alice, "send_dm", user="bob", text="Private root")
        channel_uri = f"chat://channels/{conversation['name']}"
        thread_uri = f"chat://threads/{root['id']}"
        watched = [channel_uri, thread_uri, "chat://inbox", "chat://dms"]
        bob_events = await stack.enter_async_context(listening(bob, watched, monkeypatch))
        charlie_events = await stack.enter_async_context(
            listening(charlie, [*watched, "chat://channels/general"], monkeypatch)
        )
        foreign_events = await stack.enter_async_context(
            listening(
                foreign,
                [*watched, "chat://channels/general", "chat://channels/marker"],
                monkeypatch,
            )
        )
        await call(
            alice,
            "send_dm",
            user="bob",
            text="Private reply",
            parent_id=root["id"],
            send_to_channel=True,
        )
        assert {await next_uri(bob_events) for index in range(4)} == set(watched)
        await call(alice, "open_dm", user="bob")
        assert await next_uri(bob_events) == "chat://dms"
        await call(alice, "mark_read", channel=conversation["name"], message_id=root["id"])
        await call(alice, "subscribe", channel="general")
        await call(alice, "post_message", channel="general", text="Public sentinel")
        assert await next_uri(charlie_events) == "chat://channels/general"
        await call(foreign, "create_channel", name="marker")
        await call(foreign, "post_message", channel="marker", text="Other org sentinel")
        assert await next_uri(foreign_events) == "chat://channels/marker"


@pytest.mark.parametrize("adapter", ADAPTERS, ids=["modern", "legacy"])
@pytest.mark.parametrize("direct", [False, True], ids=["public", "dm"])
async def test_thread_readers_receive_updates_without_inbox_delivery(
    tmp_path: Path, adapter: Adapter, direct: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with AsyncExitStack() as stack:
        server = await stack.enter_async_context(
            TestServer(create_app(tmp_path / "chat.sqlite3", jwt_secret=SECRET))
        )
        alice = await client_for(stack, server, adapter, "alice")
        bob = await client_for(stack, server, adapter, "bob")
        charlie = await client_for(stack, server, adapter, "charlie")
        foreign = await client_for(stack, server, adapter, "bob", "other.example")
        await call(alice, "create_channel", name="marker")
        await call(foreign, "create_channel", name="marker")
        if direct:
            root = await call(alice, "send_dm", user="bob", text="Private root")
            await call(bob, "mark_read", channel=root["channel"], message_id=root["id"])
        else:
            await call(alice, "create_channel", name="news")
            root = await call(alice, "post_message", channel="news", text="Public root")
        thread_uri = f"chat://threads/{root['id']}"
        marker_uri = "chat://channels/marker"
        watched = [thread_uri, "chat://inbox", marker_uri]
        bob_events = await stack.enter_async_context(listening(bob, watched, monkeypatch))
        charlie_events = await stack.enter_async_context(listening(charlie, watched, monkeypatch))
        foreign_events = await stack.enter_async_context(listening(foreign, watched, monkeypatch))
        reply = await call(
            alice,
            "post_message",
            channel=root["channel"],
            text="Thread-only reply",
            parent_id=root["id"],
        )
        await call(alice, "post_message", channel="marker", text="Public sentinel")
        assert await next_uri(bob_events) == thread_uri
        assert await next_uri(bob_events) == marker_uri
        if not direct:
            assert await next_uri(charlie_events) == thread_uri
        assert await next_uri(charlie_events) == marker_uri
        await call(foreign, "post_message", channel="marker", text="Other org sentinel")
        assert await next_uri(foreign_events) == marker_uri
        assert (await call(bob, "inbox"))["messages"] == []
        assert (await call(charlie, "inbox"))["messages"] == []
        thread = await call(bob, "thread", message_id=root["id"])
        assert [message["id"] for message in thread["messages"]] == [root["id"], reply["id"]]


async def test_private_filter_fails_closed_and_advances_the_cursor() -> None:
    backend = MemoryHub()
    hub = NotificationHub(backend)
    payload = {
        "jsonrpc": "2.0",
        "method": "notifications/resources/updated",
        "params": {"uri": "chat://dms"},
        RECIPIENTS: {"org": "example.com", "users": ["alice", "bob"]},
    }
    cursor = await backend.publish("private", payload)
    for actor in (None, ("example.com", "charlie"), ("other.example", "bob")):
        token = ACTOR.set(actor)
        try:
            stream = await hub.subscribe("private", after=START, wait=0)
            assert await stream.poll() == [Event(cursor, {})]
            assert stream.cursor == cursor
            assert await stream.poll() == []
        finally:
            ACTOR.reset(token)
    token = ACTOR.set(("example.com", "bob"))
    try:
        delivered = await hub.poll("private", START, timeout=0)
        assert delivered[0].message["params"]["uri"] == "chat://dms"
        assert RECIPIENTS not in delivered[0].message
    finally:
        ACTOR.reset(token)
