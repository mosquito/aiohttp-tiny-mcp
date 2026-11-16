from __future__ import annotations

from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

import pytest
from aiohttp.test_utils import TestServer
from aiohttp_tiny_mcp import CallToolResult, Client
from aiohttp_tiny_mcp.protocol.adapter import Adapter
from test_notifications import ADAPTERS, SECRET, call, client_for, listening, next_uri

from mcp_chat.server import create_app


async def ids(client: Client, tool: str = "inbox", **arguments: Any) -> list[int]:
    return [message["id"] for message in (await call(client, tool, **arguments))["messages"]]


async def refused(client: Client, tool: str, **arguments: Any) -> None:
    result = await client.call_tool(tool, arguments)
    assert isinstance(result, CallToolResult)
    assert result.is_error, result


@pytest.mark.parametrize("adapter", ADAPTERS, ids=["modern", "legacy"])
async def test_thread_reply_targets_authors_and_mentions_without_broadcast(
    tmp_path: Path, adapter: Adapter
) -> None:
    async with AsyncExitStack() as stack:
        server = await stack.enter_async_context(
            TestServer(create_app(tmp_path / "chat.sqlite3", jwt_secret=SECRET))
        )
        alice = await client_for(stack, server, adapter, "alice")
        bob = await client_for(stack, server, adapter, "bob")
        charlie = await client_for(stack, server, adapter, "charlie")
        eve = await client_for(stack, server, adapter, "eve")
        await call(alice, "create_channel", name="team")
        for client in (bob, charlie):
            await call(client, "subscribe", channel="team")
        root = await call(alice, "post_message", channel="team", text="Thread root")
        for client in (bob, charlie):
            await call(client, "mark_read", channel="team", message_id=root["id"])
        first = await call(
            bob, "post_message", channel="team", text="@eve, join this thread", parent_id=root["id"]
        )
        assert first["send_to_channel"] is False
        assert await ids(alice) == [first["id"]]
        assert await ids(charlie) == []
        assert await ids(eve) == [first["id"]]
        await call(bob, "unsubscribe", channel="team")
        followup = await call(
            alice, "post_message", channel="team", text="For the authors", parent_id=root["id"]
        )
        assert await ids(bob) == [followup["id"]]
        assert await ids(eve) == [first["id"]]
        assert await ids(charlie) == []
        assert (await call(bob, "list_subscriptions"))["subscriptions"] == []
        broadcast = await call(
            alice,
            "post_message",
            channel="team",
            text="@bob @charlie, shared reply",
            parent_id=root["id"],
            send_to_channel=True,
        )
        assert await ids(charlie) == [broadcast["id"]]
        assert await ids(bob) == [followup["id"], broadcast["id"]]
        assert await ids(eve) == [first["id"]]
        history = await call(charlie, "history", channel="team")
        assert [message["id"] for message in history["messages"]] == [root["id"], broadcast["id"]]
        assert history["messages"][0]["reply_count"] == 3
        assert await ids(charlie, "thread", message_id=root["id"]) == [
            root["id"],
            first["id"],
            followup["id"],
            broadcast["id"],
        ]
        await call(bob, "mark_read", channel="team", message_ids=[broadcast["id"], broadcast["id"]])
        assert await ids(bob) == [followup["id"]]
        await call(bob, "subscribe", channel="team")
        assert await ids(bob) == [followup["id"]]
        await call(
            bob, "mark_read", channel="team", message_ids=[root["id"], first["id"], followup["id"]]
        )
        assert await ids(bob) == []


@pytest.mark.parametrize("adapter", ADAPTERS, ids=["modern", "legacy"])
async def test_exact_acknowledgement_preserves_other_threads_and_is_atomic(
    tmp_path: Path, adapter: Adapter
) -> None:
    async with AsyncExitStack() as stack:
        server = await stack.enter_async_context(
            TestServer(create_app(tmp_path / "chat.sqlite3", jwt_secret=SECRET))
        )
        alice = await client_for(stack, server, adapter, "alice")
        bob = await client_for(stack, server, adapter, "bob")
        foreign = await client_for(stack, server, adapter, "alice", "other.example")
        await call(alice, "create_channel", name="team")
        await call(alice, "create_channel", name="other")
        await call(bob, "subscribe", channel="team")
        roots = [
            await call(alice, "post_message", channel="team", text=f"Root {number}")
            for number in range(3)
        ]
        await call(bob, "mark_read", channel="team", message_id=roots[2]["id"])
        assert await ids(bob) == [roots[0]["id"], roots[1]["id"]]
        wrong_channel = await call(alice, "post_message", channel="other", text="Another channel")
        await call(foreign, "create_channel", name="team")
        wrong_org = await call(foreign, "post_message", channel="team", text="Another org")
        private = await call(alice, "send_dm", user="charlie", text="Private")
        for invalid_id in (wrong_channel["id"], wrong_org["id"], private["id"], 999999):
            await refused(
                bob, "mark_read", channel="team", message_ids=[roots[0]["id"], invalid_id]
            )
            assert await ids(bob) == [roots[0]["id"], roots[1]["id"]]
        await call(bob, "mark_read", channel="team", message_ids=[roots[0]["id"], roots[2]["id"]])
        assert await ids(bob) == [roots[1]["id"]]
        await call(bob, "mark_read", channel="team", message_id=roots[0]["id"])
        assert await ids(bob) == [roots[1]["id"]]


@pytest.mark.parametrize("adapter", ADAPTERS, ids=["modern", "legacy"])
async def test_thread_inbox_notifications_are_recipient_scoped(
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
        await call(alice, "create_channel", name="team")
        await call(alice, "create_channel", name="marker")
        await call(foreign, "create_channel", name="marker")
        for client in (bob, charlie):
            await call(client, "subscribe", channel="team")
        root = await call(alice, "post_message", channel="team", text="Root")
        await call(bob, "post_message", channel="team", text="Participate", parent_id=root["id"])
        watched = ["chat://inbox", "chat://channels/marker"]
        bob_events = await stack.enter_async_context(listening(bob, watched, monkeypatch))
        charlie_events = await stack.enter_async_context(listening(charlie, watched, monkeypatch))
        foreign_events = await stack.enter_async_context(listening(foreign, watched, monkeypatch))
        reply = await call(
            alice, "post_message", channel="team", text="Reply to authors", parent_id=root["id"]
        )
        assert await next_uri(bob_events) == "chat://inbox"
        await call(alice, "post_message", channel="marker", text="Sentinel")
        assert await next_uri(bob_events) == "chat://channels/marker"
        assert await next_uri(charlie_events) == "chat://channels/marker"
        await call(foreign, "post_message", channel="marker", text="Foreign sentinel")
        assert await next_uri(foreign_events) == "chat://channels/marker"
        assert reply["id"] not in await ids(charlie)
        await call(
            alice,
            "post_message",
            channel="team",
            text="Also notify members",
            parent_id=root["id"],
            send_to_channel=True,
        )
        assert await next_uri(bob_events) == "chat://inbox"
        assert await next_uri(charlie_events) == "chat://inbox"


@pytest.mark.parametrize("adapter", ADAPTERS, ids=["modern", "legacy"])
async def test_dm_thread_routing_never_adds_a_third_recipient(
    tmp_path: Path, adapter: Adapter
) -> None:
    async with AsyncExitStack() as stack:
        server = await stack.enter_async_context(
            TestServer(create_app(tmp_path / "chat.sqlite3", jwt_secret=SECRET))
        )
        alice = await client_for(stack, server, adapter, "alice")
        bob = await client_for(stack, server, adapter, "bob")
        charlie = await client_for(stack, server, adapter, "charlie")
        root = await call(alice, "send_dm", user="bob", text="DM root")
        await call(bob, "mark_read", channel=root["channel"], message_id=root["id"])
        hidden = await call(
            alice, "send_dm", user="bob", text="@charlie, thread note", parent_id=root["id"]
        )
        assert await ids(bob) == []
        assert await ids(charlie) == []
        broadcast = await call(
            alice,
            "send_dm",
            user="bob",
            text="@charlie, shared note",
            parent_id=root["id"],
            send_to_channel=True,
        )
        assert await ids(bob) == [broadcast["id"]]
        assert await ids(charlie) == []
        assert await ids(bob, "history", channel=root["channel"]) == [root["id"], broadcast["id"]]
        assert await ids(bob, "thread", message_id=root["id"]) == [
            root["id"],
            hidden["id"],
            broadcast["id"],
        ]
        await refused(charlie, "thread", message_id=root["id"])
        await refused(charlie, "mark_read", channel=root["channel"], message_ids=[broadcast["id"]])


@pytest.mark.parametrize("bad", ["true", 1, None])
async def test_send_to_channel_requires_a_boolean(tmp_path: Path, bad: Any) -> None:
    async with AsyncExitStack() as stack:
        server = await stack.enter_async_context(
            TestServer(create_app(tmp_path / "chat.sqlite3", jwt_secret=SECRET))
        )
        alice = await client_for(stack, server, ADAPTERS[0], "alice")
        await call(alice, "create_channel", name="team")
        root = await call(alice, "post_message", channel="team", text="Root")
        await refused(
            alice,
            "post_message",
            channel="team",
            text="Invalid flag",
            parent_id=root["id"],
            send_to_channel=bad,
        )
        private = await call(alice, "send_dm", user="bob", text="DM root")
        await refused(
            alice,
            "send_dm",
            user="bob",
            text="Invalid flag",
            parent_id=private["id"],
            send_to_channel=bad,
        )


@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"message_id": 1, "message_ids": [1]},
        {"message_ids": []},
        {"message_ids": [1] * 101},
        {"message_ids": [0]},
    ],
)
async def test_mark_read_requires_exactly_one_bounded_selector(
    tmp_path: Path, arguments: dict[str, Any]
) -> None:
    async with AsyncExitStack() as stack:
        server = await stack.enter_async_context(
            TestServer(create_app(tmp_path / "chat.sqlite3", jwt_secret=SECRET))
        )
        alice = await client_for(stack, server, ADAPTERS[0], "alice")
        await call(alice, "create_channel", name="team")
        await call(alice, "post_message", channel="team", text="Root")
        await refused(alice, "mark_read", channel="team", **arguments)
