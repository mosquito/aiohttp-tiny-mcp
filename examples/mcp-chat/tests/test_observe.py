from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

import jwt
import pytest
from aiohttp.test_utils import TestClient, TestServer
from aiohttp_tiny_mcp import AdapterSet, CallToolResult, Client, ClientError

from mcp_chat.auth import WEB_AUDIENCE, WEB_COOKIE, issue_token
from mcp_chat.server import create_app
from mcp_chat.store import Store

SECRET = "test-only-observe-signing-key-longer-than-thirty-two-bytes"
MODERN = AdapterSet.default().by_version["2026-07-28"]
LEGACY = AdapterSet.default().by_version["2025-11-25"]


@pytest.mark.parametrize("adapter", [MODERN, LEGACY], ids=["modern", "legacy"])
async def test_observe_records_real_calls_and_resource_reads(tmp_path: Path, adapter: Any) -> None:
    path = tmp_path / "chat.sqlite3"
    async with TestClient(TestServer(create_app(path))) as http:
        async with Client(str(http.make_url("/mcp?org=example.com&user=alice")), adapter) as alice:
            await alice.call_tool("subscribe", {"channel": "general"})
            await alice.call_tool("post_message", {"channel": "general", "text": "Ship tomorrow"})
            await alice.call_tool("send_dm", {"user": "bob", "text": "Private rollout"})
            await alice.call_tool("unsubscribe", {"channel": "general"})
            failure = await alice.call_tool("post_message", {"channel": "general", "text": "No"})
            assert isinstance(failure, CallToolResult) and failure.is_error
            invalid = await alice.call_tool("history", {"channel": "general", "limit": 0})
            assert isinstance(invalid, CallToolResult) and invalid.is_error
            await alice.read_resource("chat://dms")
            with pytest.raises(ClientError):
                await alice.read_resource("chat://channels/missing")
        async with Client(str(http.make_url("/mcp?org=example.com&user=bob")), adapter) as bob:
            await bob.call_tool("inbox", {})
        async with Client(str(http.make_url("/mcp?org=other.com&user=alice")), adapter) as other:
            await other.call_tool("identity", {})
        response = await http.get("/admin/observe?user=alice")
        assert response.status == 200
        assert response.headers["Cache-Control"] == "no-store"
        data = await response.json()
        events = data["events"]
        assert len(events) == 8
        assert all(row["user"] == "alice" and row["org"] == "example.com" for row in events)
        assert all(row["created_at"].endswith("Z") for row in events)
        assert [row["id"] for row in events] == sorted((row["id"] for row in events), reverse=True)
        assert [row["tool"] for row in events] == [
            "resources/read",
            "resources/read",
            "history",
            "post_message",
            "unsubscribe",
            "send_dm",
            "post_message",
            "subscribe",
        ]
        assert all(events[index]["error"] for index in [0, 2, 3])
        assert events[1]["arguments"] == {"uri": "chat://dms"}
        assert events[5]["arguments"] == {"user": "bob", "text": "Private rollout"}
        assert events[5]["result"]["structuredContent"]["text"] == "Private rollout"
        assert events[6]["arguments"]["text"] == "Ship tomorrow"
        assert events[-1]["arguments"] == {"channel": "general"}
        assert data["users"] == ["alice", "bob"]
        assert data["orgs"] == ["example.com", "other.com"]
        assert data["has_more"] is False and data["next_before"] is None
        assert await (await http.get("/admin/observe?user=alice")).json() == data
        assert len((await (await http.get("/admin/observe")).json())["events"]) == 9
        other_data = await (await http.get("/admin/observe?org=other.com")).json()
        assert len(other_data["events"]) == 1
    store = Store(path)
    await store.open()
    try:
        assert len((await store.observe("example.com", "alice"))["events"]) == 8
    finally:
        await store.close()


async def test_observe_pagination_and_secret_redaction(tmp_path: Path) -> None:
    async with TestClient(TestServer(create_app(tmp_path / "chat.sqlite3"))) as http:
        async with Client(str(http.make_url("/mcp?org=example.com&user=alice")), MODERN) as client:
            for index in range(5):
                result = await client.call_tool("identity", {})
                assert isinstance(result, CallToolResult), index
            await client.call_tool("identity", {"token": "credential-never-store"})
        first = await (await http.get("/admin/observe?limit=2")).json()
        second = await (
            await http.get(
                "/admin/observe", params={"limit": "2", "before": str(first["next_before"])}
            )
        ).json()
        third = await (
            await http.get(
                "/admin/observe", params={"limit": "2", "before": str(second["next_before"])}
            )
        ).json()
        ids = [event["id"] for page in [first, second, third] for event in page["events"]]
        assert len(ids) == len(set(ids)) == 6
        assert first["has_more"] and second["has_more"] and not third["has_more"]
        assert third["next_before"] is None
        assert "credential-never-store" not in json.dumps(first)
        assert first["events"][0]["arguments"]["token"] == "[redacted]"


@pytest.mark.parametrize(
    "query",
    [
        "limit=0",
        "limit=101",
        "limit=1.5",
        "limit=-1",
        "before=0",
        "before=-1",
        "before=x",
        "before=99999999999999999999",
        "limit=1&limit=2",
        "org=x&org=y",
        "org=../x",
        "user=",
    ],
)
async def test_observe_rejects_invalid_queries(tmp_path: Path, query: str) -> None:
    async with TestClient(TestServer(create_app(tmp_path / "chat.sqlite3"))) as client:
        response = await client.get(f"/admin/observe?{query}")
        assert response.status == 400
        assert response.headers["Cache-Control"] == "no-store"


async def test_observe_requires_admin_cookie_and_ignores_mcp_tokens(tmp_path: Path) -> None:
    async with TestClient(
        TestServer(create_app(tmp_path / "chat.sqlite3", jwt_secret=SECRET))
    ) as client:
        token = issue_token(SECRET, "example.com", "alice")
        for headers in [
            {},
            {"Authorization": f"Bearer {token}"},
            {"Cookie": f"{WEB_COOKIE}={token}"},
        ]:
            response = await client.get("/admin/observe", headers=headers)
            assert response.status == 401
        expired = jwt.encode(
            {
                "sub": "web-operator",
                "iss": "mcp-chat",
                "aud": WEB_AUDIENCE,
                "iat": int(time.time()) - 100,
                "exp": int(time.time()) - 1,
            },
            SECRET,
            algorithm="HS256",
        )
        response = await client.get("/admin/observe", headers={"Cookie": f"{WEB_COOKIE}={expired}"})
        assert response.status == 401
        response = await client.post(
            "/admin/auth", json={"secret": SECRET, "org": "example.com", "user": "alice"}
        )
        assert response.status == 200
        response = await client.get("/admin/observe")
        assert response.status == 200
        assert (await response.json())["events"] == []
        assert SECRET not in await response.text()


async def test_observe_stream_filters_and_detects_other_connections(tmp_path: Path) -> None:
    path = tmp_path / "chat.sqlite3"
    async with TestClient(TestServer(create_app(path))) as http:
        response = await http.get("/admin/observe?org=example.com&user=alice&stream=true")
        assert response.status == 200
        assert response.content_type == "text/event-stream"
        assert await response.content.readuntil(b"\n\n") == b"event: ready\ndata: {}\n\n"
        async with Client(str(http.make_url("/mcp?org=example.com&user=alice")), MODERN) as alice:
            await alice.call_tool("identity", {})
        assert await asyncio.wait_for(response.content.readuntil(b"\n\n"), 2) == (
            b"event: audit\ndata: {}\n\n"
        )
        external = Store(path)
        await external.open()
        try:
            await external.record_event("other.com", "alice", "identity", {}, {}, None)
            await external.record_event("example.com", "bob", "identity", {}, {}, None)
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(response.content.readuntil(b"\n\n"), 1.2)
            await external.record_event("example.com", "alice", "identity", {}, {}, None)
            assert await asyncio.wait_for(response.content.readuntil(b"\n\n"), 2) == (
                b"event: audit\ndata: {}\n\n"
            )
        finally:
            await external.close()
            response.close()


async def test_observe_stream_closes_when_admin_cookie_expires(tmp_path: Path) -> None:
    cookie = jwt.encode(
        {
            "sub": "web-operator",
            "iss": "mcp-chat",
            "aud": WEB_AUDIENCE,
            "iat": int(time.time()),
            "exp": time.time() + 2,
        },
        SECRET,
        algorithm="HS256",
    )
    async with TestClient(
        TestServer(create_app(tmp_path / "chat.sqlite3", jwt_secret=SECRET))
    ) as client:
        response = await client.get(
            "/admin/observe?stream=true", headers={"Cookie": f"{WEB_COOKIE}={cookie}"}
        )
        assert response.status == 200
        assert await response.content.readuntil(b"\n\n") == b"event: ready\ndata: {}\n\n"
        assert await asyncio.wait_for(response.content.read(), 4) == b""


async def test_observe_resolves_dm_recipient_for_each_recorded_actor(tmp_path: Path) -> None:
    path = tmp_path / "chat.sqlite3"
    async with TestClient(TestServer(create_app(path))) as http:
        async with (
            Client(str(http.make_url("/mcp?org=example.com&user=alice")), MODERN) as alice,
            Client(str(http.make_url("/mcp?org=example.com&user=bob")), MODERN) as bob,
            Client(str(http.make_url("/mcp?org=example.com&user=carol")), MODERN) as carol,
            Client(str(http.make_url("/mcp?org=other.com&user=alice")), MODERN) as other,
        ):
            sent = await alice.call_tool("send_dm", {"user": "bob", "text": "Private greeting"})
            assert isinstance(sent, CallToolResult)
            message = sent.structured_content
            assert isinstance(message, dict)
            channel = message["channel"]
            reply_args = {"channel": channel, "text": "Private reply"}
            reply = await bob.call_tool("post_message", reply_args)
            assert isinstance(reply, CallToolResult) and not reply.is_error
            await carol.call_tool("post_message", {"channel": channel, "text": "Denied"})
            await other.call_tool("post_message", {"channel": channel, "text": "Wrong org"})
        page = await (await http.get("/admin/observe")).json()
        carol_event, bob_event, alice_event = page["events"]
        assert alice_event["conversation"] == {"kind": "dm", "peer": "bob", "channel": channel}
        assert bob_event["conversation"] == {"kind": "dm", "peer": "alice", "channel": channel}
        assert bob_event["arguments"] == reply_args
        assert bob_event["result"]["structuredContent"] == reply.structured_content
        assert alice_event["arguments"] == {"user": "bob", "text": "Private greeting"}
        assert alice_event["result"]["structuredContent"] == message
        assert carol_event["error"] and "conversation" not in carol_event
        other_page = await (await http.get("/admin/observe?org=other.com")).json()
        assert "conversation" not in other_page["events"][0]
        bob_page = await (await http.get("/admin/observe?user=bob")).json()
        assert bob_page["events"][0]["conversation"]["peer"] == "alice"
    store = Store(path)
    await store.open()
    try:
        async with store.transaction() as connection:
            rows = await store.rows(
                connection, "SELECT arguments, result FROM audit_events WHERE user = ?", ("bob",)
            )
        assert json.loads(rows[0]["arguments"]) == reply_args
        assert json.loads(rows[0]["result"])["structuredContent"] == reply.structured_content
    finally:
        await store.close()


async def test_observe_tool_filters_apply_before_pagination(tmp_path: Path) -> None:
    path = tmp_path / "chat.sqlite3"
    async with TestClient(TestServer(create_app(path))) as http:
        store = Store(path)
        await store.open()
        try:
            for name in [
                "post_message",
                "identity",
                "send_dm",
                "identity",
                "post_message",
                "identity",
                "resources/read",
                "custom.tool-v2",
                "identity",
            ]:
                await store.record_event("example.com", "alice", name, {}, {}, None)
            first = await (
                await http.get(
                    "/admin/observe?tool=post_message&tool=send_dm&tool=post_message&limit=2"
                )
            ).json()
            assert [event["tool"] for event in first["events"]] == ["post_message", "send_dm"]
            assert first["has_more"] is True
            second = await (
                await http.get(
                    "/admin/observe",
                    params=[
                        ("tool", "post_message"),
                        ("tool", "send_dm"),
                        ("limit", "2"),
                        ("before", str(first["next_before"])),
                    ],
                )
            ).json()
            assert [event["tool"] for event in second["events"]] == ["post_message"]
            assert second["has_more"] is False and second["next_before"] is None
            all_events = await (await http.get("/admin/observe")).json()
            assert len(all_events["events"]) == 9
            reads = await (await http.get("/admin/observe?tool=resources/read")).json()
            assert [event["tool"] for event in reads["events"]] == ["resources/read"]
            custom = await (await http.get("/admin/observe?tool=custom.tool-v2")).json()
            assert [event["tool"] for event in custom["events"]] == ["custom.tool-v2"]
            absent = await (await http.get("/admin/observe?tool=absent")).json()
            assert absent["events"] == [] and absent["has_more"] is False
            assert await store.latest_event("example.com", "alice", tools=("send_dm",)) == 3
            assert await store.latest_event("example.com", "alice", tools=[]) == 0
            assert (await store.observe("example.com", tools=[]))["events"] == []
        finally:
            await store.close()


@pytest.mark.parametrize(
    "query",
    [
        "tool=",
        "tool=post%20message",
        "tool=send_dm%27",
        "tool=/resources/read",
        "tool=resources//read",
        "tool=resources/read/",
        "tool=" + "a" * 129,
        "&".join(["tool=identity"] * 33),
        "tool=identity&user=a&user=b",
    ],
)
@pytest.mark.parametrize("stream", [False, True], ids=["json", "sse"])
async def test_observe_rejects_invalid_tool_filters(
    tmp_path: Path, query: str, stream: bool
) -> None:
    async with TestClient(TestServer(create_app(tmp_path / "chat.sqlite3"))) as client:
        response = await client.get(f"/admin/observe?{query}&stream={str(stream).lower()}")
        assert response.status == 400
        assert response.headers["Cache-Control"] == "no-store"


async def test_observe_stream_tool_filters_local_and_external_events(tmp_path: Path) -> None:
    path = tmp_path / "chat.sqlite3"
    async with TestClient(TestServer(create_app(path))) as http:
        response = await http.get(
            "/admin/observe?org=example.com&user=alice&stream=true&tool=send_dm&tool=post_message"
        )
        assert response.status == 200
        assert await response.content.readuntil(b"\n\n") == b"event: ready\ndata: {}\n\n"
        external = Store(path)
        await external.open()
        try:
            async with Client(
                str(http.make_url("/mcp?org=example.com&user=alice")), MODERN
            ) as alice:
                await alice.call_tool("identity", {})
                await external.record_event("example.com", "alice", "inbox", {}, {}, None)
                await external.record_event("example.com", "bob", "send_dm", {}, {}, None)
                await external.record_event("other.com", "alice", "send_dm", {}, {}, None)
                with pytest.raises(asyncio.TimeoutError):
                    await asyncio.wait_for(response.content.readuntil(b"\n\n"), 1.2)
                await alice.call_tool("send_dm", {"user": "bob", "text": "Matching event"})
                assert await asyncio.wait_for(response.content.readuntil(b"\n\n"), 2) == (
                    b"event: audit\ndata: {}\n\n"
                )
            await external.record_event("example.com", "alice", "post_message", {}, {}, None)
            assert await asyncio.wait_for(response.content.readuntil(b"\n\n"), 2) == (
                b"event: audit\ndata: {}\n\n"
            )
        finally:
            await external.close()
            response.close()
