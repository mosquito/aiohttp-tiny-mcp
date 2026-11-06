from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import aiohttp
import aiosqlite
import pytest
from aiohttp.test_utils import BaseTestServer, TestServer
from aiohttp_tiny_mcp import AdapterSet, CallToolResult, Client, ClientError, TextResourceContents

from mcp_chat.auth import issue_token
from mcp_chat.notifications import RECIPIENTS
from mcp_chat.server import create_app
from mcp_chat.store import Store

MODERN = AdapterSet.default().by_version["2026-07-28"]
LEGACY = AdapterSet.default().by_version["2025-11-25"]
SECRET = "test-only-secret-for-signed-direct-message-identities"


def endpoint(server: BaseTestServer, user: str, org: str = "example.com") -> str:
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


async def read_json(client: Client, uri: str) -> dict[str, Any]:
    content = (await client.read_resource(uri)).contents[0]
    assert isinstance(content, TextResourceContents)
    return json.loads(content.text)


@pytest.mark.parametrize("adapter", [MODERN, LEGACY], ids=["modern", "legacy"])
async def test_direct_message_workflow_and_privacy(tmp_path: Path, adapter: Any) -> None:
    async with TestServer(create_app(tmp_path / "chat.sqlite3")) as server:
        async with (
            Client(endpoint(server, "alice"), adapter) as alice,
            Client(endpoint(server, "bob"), adapter) as bob,
            Client(endpoint(server, "charlie"), adapter) as charlie,
            Client(endpoint(server, "alice", "other.example"), adapter) as other,
        ):
            opened = await call(alice, "open_dm", user="bob")
            channel = opened["name"]
            assert opened["kind"] == "dm"
            assert opened["peer"] == "bob"
            assert opened["subscribed"] is True
            assert (await call(bob, "open_dm", user="alice"))["name"] == channel
            root = await call(alice, "send_dm", user="bob", text="Private release discussion")
            assert root["channel"] == channel
            reply = await call(
                bob, "send_dm", user="alice", text="Private reply", parent_id=root["id"]
            )
            assert [m["id"] for m in (await call(alice, "inbox"))["messages"]] == [reply["id"]]
            assert [m["id"] for m in (await call(bob, "inbox"))["messages"]] == [root["id"]]
            direct = (await call(alice, "list_dms"))["conversations"][0]
            assert direct["peer"] == "bob"
            assert direct["unread"] == 1
            assert (await read_json(bob, "chat://dms"))["conversations"][0]["peer"] == "alice"
            assert [
                m["id"] for m in (await call(bob, "thread", message_id=root["id"]))["messages"]
            ] == [root["id"], reply["id"]]
            assert len((await call(bob, "search", query="private"))["messages"]) == 2
            assert (await read_json(alice, f"chat://channels/{channel}"))["messages"][0][
                "id"
            ] == root["id"]
            await call(bob, "mark_read", channel=channel, message_id=root["id"])
            assert (await call(bob, "inbox"))["messages"] == []
            await refused(alice, "subscribe", channel=channel)
            await refused(alice, "unsubscribe", channel=channel)
            await refused(alice, "open_dm", user="alice")
            await refused(alice, "send_dm", user="../bob", text="Invalid recipient")
            for client in (alice, bob, charlie, other):
                assert channel not in str((await call(client, "list_channels"))["channels"])
                assert channel not in str(
                    (await call(client, "list_subscriptions"))["subscriptions"]
                )
            for outsider in (charlie, other):
                assert (await call(outsider, "list_dms"))["conversations"] == []
                assert (await call(outsider, "inbox"))["messages"] == []
                assert (await call(outsider, "search", query="private"))["messages"] == []
                await refused(outsider, "history", channel=channel)
                await refused(outsider, "thread", message_id=root["id"])
                await refused(outsider, "post_message", channel=channel, text="Intrusion")
                await refused(outsider, "mark_read", channel=channel, message_id=root["id"])
                await refused(outsider, "subscribe", channel=channel)
                await refused(outsider, "unsubscribe", channel=channel)
                await refused(outsider, "search", query="private", channel=channel)
                for uri in [
                    f"chat://channels/{channel}",
                    f"chat://channels/{channel}/messages/0",
                    f"chat://threads/{root['id']}",
                    f"chat://threads/{root['id']}/messages/0",
                ]:
                    with pytest.raises(ClientError):
                        await outsider.read_resource(uri)
            await refused(charlie, "send_dm", user="bob", text="Wrong thread", parent_id=root["id"])
            assert (await call(charlie, "open_dm", user="bob"))["name"] != channel


async def test_direct_message_persistence_concurrency_and_public_only_defaults(
    tmp_path: Path,
) -> None:
    path = tmp_path / "chat.sqlite3"
    first, second = Store(path), Store(path)
    await first.open()
    await second.open()
    try:
        opened = await asyncio.gather(
            *[
                storage.open_dm("example.com", user, peer)
                for storage, user, peer in [(first, "alice", "bob"), (second, "bob", "alice")] * 6
            ]
        )
        assert len({row["name"] for row in opened}) == 1
        channel = opened[0]["name"]
        message = await first.send_dm("example.com", "alice", "bob", "Private persisted")
        assert await first.search("example.com", "private") == []
        with pytest.raises(ValueError):
            await first.history("example.com", channel)
        with pytest.raises(ValueError):
            await first.thread("example.com", message["id"])
        async with first.transaction() as connection:
            await connection.execute(
                "INSERT INTO subscriptions(org,user,channel) VALUES (?,?,?)",
                ("example.com", "charlie", channel),
            )
        assert await first.inbox("example.com", "charlie") == []
        assert await first.list_dms("example.com", "charlie") == []
        with pytest.raises(ValueError):
            await first.post_message("example.com", "charlie", channel, "Stale subscription")
    finally:
        await first.close()
        await second.close()
    await first.open()
    try:
        assert (await first.list_dms("example.com", "bob"))[0]["name"] == channel
        assert [row["id"] for row in await first.inbox("example.com", "bob")] == [message["id"]]
        assert [
            row["id"] for row in await first.search("example.com", "persisted", user="bob")
        ] == [message["id"]]
        await first.mark_read("example.com", "bob", channel, message["id"])
    finally:
        await first.close()


async def test_jwt_query_spoof_cannot_read_another_users_dm(tmp_path: Path) -> None:
    async with TestServer(create_app(tmp_path / "chat.sqlite3", jwt_secret=SECRET)) as server:
        async with (
            aiohttp.ClientSession(
                headers={"Authorization": f"Bearer {issue_token(SECRET, 'example.com', 'bob')}"}
            ) as bob_session,
            aiohttp.ClientSession(
                headers={"Authorization": f"Bearer {issue_token(SECRET, 'example.com', 'alice')}"}
            ) as alice_session,
        ):
            async with (
                Client(str(server.make_url("/mcp")), MODERN, session=bob_session) as bob,
                Client(
                    str(server.make_url("/mcp?org=other.example&user=bob&user=carol")),
                    MODERN,
                    session=alice_session,
                ) as alice,
            ):
                message = await call(bob, "send_dm", user="carol", text="Private JWT discussion")
                assert await call(alice, "identity") == {"org": "example.com", "user": "alice"}
                assert (await call(alice, "search", query="private"))["messages"] == []
                assert (await call(alice, "list_dms"))["conversations"] == []
                await refused(alice, "history", channel=message["channel"])
                await refused(alice, "thread", message_id=message["id"])


async def test_direct_messages_publish_only_recipient_scoped_events(tmp_path: Path) -> None:
    path = tmp_path / "chat.sqlite3"
    async with TestServer(create_app(path)) as server:
        async with Client(endpoint(server, "alice"), MODERN) as client:
            await call(client, "subscribe", channel="general")
            await call(client, "post_message", channel="general", text="Public message")
            async with aiosqlite.connect(path) as connection:
                async with connection.execute("SELECT count(*) FROM mcp_events") as cursor:
                    row = await cursor.fetchone()
                    assert row is not None
                    before = row[0]
                assert before > 0
                opened = await call(client, "open_dm", user="bob")
                await call(client, "send_dm", user="bob", text="Private message")
                await call(client, "post_message", channel=opened["name"], text="Private follow-up")
                async with connection.execute(
                    "SELECT message FROM mcp_events WHERE id > ?", (before,)
                ) as cursor:
                    events = [json.loads(row[0]) for row in await cursor.fetchall()]
                assert events
                for event in events:
                    assert event[RECIPIENTS]["org"] == "example.com"
                    assert set(event[RECIPIENTS]["users"]) <= {"alice", "bob"}
                    assert "Private message" not in str(event)
