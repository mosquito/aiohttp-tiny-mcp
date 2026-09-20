"""Session-store contract, endpoint integration, and explicit session handles."""

from __future__ import annotations

import pytest
from aiohttp.test_utils import TestClient, TestServer

from aiohttp_tiny_mcp import Endpoint, Exchange, MemorySessionStore, Registry, namespace
from aiohttp_tiny_mcp.protocol.selection import AdapterSet
from aiohttp_tiny_mcp.sessions import SESSION_HEADER, new_session_id

pytestmark = pytest.mark.asyncio


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


async def test_create_then_get_round_trips():
    store = MemorySessionStore()
    assert await store.create("s1", {"a": 1}, ttl_seconds=60) is True
    record = await store.get("s1")
    assert record is not None
    assert record.data == {"a": 1}
    assert record.version == 1


async def test_get_unknown_session_is_none():
    assert await MemorySessionStore().get("nope") is None


async def test_create_refuses_to_overwrite():
    store = MemorySessionStore()
    await store.create("s1", {"a": 1}, ttl_seconds=60)
    assert await store.create("s1", {"a": 2}, ttl_seconds=60) is False
    record = await store.get("s1")
    assert record is not None and record.data == {"a": 1}


async def test_save_requires_the_version_it_read():
    store = MemorySessionStore()
    await store.create("s1", {"a": 1}, ttl_seconds=60)
    assert await store.save("s1", {"a": 2}, expected_version=1, ttl_seconds=60) is True
    record = await store.get("s1")
    assert record is not None and record.version == 2


async def test_save_rejects_a_lost_update():
    """Two writers read the same version; only the first write may succeed."""
    store = MemorySessionStore()
    await store.create("s1", {"a": 1}, ttl_seconds=60)
    assert await store.save("s1", {"a": 2}, expected_version=1, ttl_seconds=60) is True
    assert await store.save("s1", {"a": 3}, expected_version=1, ttl_seconds=60) is False
    record = await store.get("s1")
    assert record is not None and record.data == {"a": 2}


async def test_save_on_unknown_session_fails():
    store = MemorySessionStore()
    assert await store.save("gone", {"a": 1}, expected_version=1, ttl_seconds=60) is False


async def test_delete_is_idempotent():
    store = MemorySessionStore()
    await store.create("s1", {"a": 1}, ttl_seconds=60)
    await store.delete("s1")
    await store.delete("s1")
    assert await store.get("s1") is None


async def test_sessions_expire():
    clock = Clock()
    store = MemorySessionStore(clock=clock)
    await store.create("s1", {"a": 1}, ttl_seconds=10)
    clock.now = 9
    assert await store.get("s1") is not None
    clock.now = 10
    assert await store.get("s1") is None


async def test_touch_moves_the_expiry_and_nothing_else():
    clock = Clock()
    store = MemorySessionStore(clock=clock)
    await store.create("s1", {"a": 1}, ttl_seconds=10)
    clock.now = 9
    assert await store.touch("s1", ttl_seconds=10) is True
    clock.now = 18
    record = await store.get("s1")
    assert record is not None
    assert record.data == {"a": 1}
    assert record.version == 1
    clock.now = 19
    assert await store.get("s1") is None


async def test_touch_reports_a_missing_or_expired_session():
    clock = Clock()
    store = MemorySessionStore(clock=clock)
    assert await store.touch("never", ttl_seconds=10) is False
    await store.create("s1", {"a": 1}, ttl_seconds=10)
    clock.now = 10
    assert await store.touch("s1", ttl_seconds=10) is False
    assert await store.get("s1") is None


async def test_every_store_touches_a_live_record_only(store_and_hub):
    """The contract, on each backend that answers: True keeps data and version,
    False names a record the store does not hold."""
    store, _ = store_and_hub
    session_id = f"touch-{new_session_id()}"
    await store.create(session_id, {"a": 1}, ttl_seconds=60)
    await store.save(session_id, {"a": 2}, expected_version=1, ttl_seconds=60)
    assert await store.touch(session_id, ttl_seconds=60) is True
    record = await store.get(session_id)
    assert record is not None
    assert record.data == {"a": 2}
    assert record.version == 2
    assert await store.touch(f"never-{new_session_id()}", ttl_seconds=60) is False


async def initialize(client, version="2025-06-18", **kw):
    return await client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": version, "capabilities": {"elicitation": {}}},
        },
        **kw,
    )


async def stateful_client(registry: Registry) -> TestClient:
    registry.session_store = MemorySessionStore()
    endpoint = Endpoint(registry)
    client = TestClient(TestServer(endpoint.app("/mcp")))
    await client.start_server()
    return client


async def test_handshake_is_remembered_when_a_store_is_supplied(registry: Registry):
    client = await stateful_client(registry)
    try:
        resp = await initialize(client, "2025-06-18")
        session_id = resp.headers.get(SESSION_HEADER)
        assert session_id

        record = await registry.session_store.get(session_id)
        assert record is not None
        assert record.data["protocolVersion"] == "2025-06-18"
        assert record.data["capabilities"] == {"elicitation": {}}
    finally:
        await client.close()


async def test_remembered_revision_is_used_for_later_requests(registry: Registry):
    """Without remembered negotiation, a bare legacy request would fall back to 2025-03-26."""
    client = await stateful_client(registry)
    try:
        session_id = (await initialize(client, "2025-03-26")).headers[SESSION_HEADER]

        batched = await client.post(
            "/mcp",
            headers={SESSION_HEADER: session_id},
            json=[{"jsonrpc": "2.0", "id": 2, "method": "ping", "params": {}}],
        )
        assert batched.status == 200

        other = await initialize(client, "2025-11-25")
        rejected = await client.post(
            "/mcp",
            headers={SESSION_HEADER: other.headers[SESSION_HEADER]},
            json=[{"jsonrpc": "2.0", "id": 2, "method": "ping", "params": {}}],
        )
        body = await rejected.json()
        assert body["error"]["code"] == -32600
    finally:
        await client.close()


async def test_explicit_version_outranks_the_session(registry: Registry):
    client = await stateful_client(registry)
    try:
        session_id = (await initialize(client, "2025-03-26")).headers[SESSION_HEADER]
        resp = await client.post(
            "/mcp",
            headers={SESSION_HEADER: session_id, "MCP-Protocol-Version": "2025-11-25"},
            json=[{"jsonrpc": "2.0", "id": 2, "method": "ping", "params": {}}],
        )
        body = await resp.json()
        assert body["error"]["code"] == -32600  # batching, refused by 2025-11-25
    finally:
        await client.close()


async def test_an_unknown_session_id_is_404(registry: Registry):
    """The spec answers a session the server no longer holds with 404, so the
    client starts over with initialize instead of going on without its
    capabilities and log level."""
    client = await stateful_client(registry)
    try:
        resp = await client.post(
            "/mcp",
            headers={SESSION_HEADER: "no-such-session"},
            json={"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {}},
        )
        assert resp.status == 404
        body = await resp.json()
        assert body["jsonrpc"] == "2.0"
        assert body["error"]["code"] == -32001
        assert "session" in body["error"]["message"]
    finally:
        await client.close()


async def test_an_expired_session_id_is_404(registry: Registry):
    clock = Clock()
    registry.session_store = MemorySessionStore(clock=clock)
    registry.session_ttl_seconds = 10
    endpoint = Endpoint(registry)
    client = TestClient(TestServer(endpoint.app("/mcp")))
    await client.start_server()
    try:
        session_id = (await initialize(client, "2025-11-25")).headers[SESSION_HEADER]
        clock.now = 10
        resp = await client.post(
            "/mcp",
            headers={SESSION_HEADER: session_id},
            json={"jsonrpc": "2.0", "id": 2, "method": "ping", "params": {}},
        )
        assert resp.status == 404
    finally:
        await client.close()


async def test_the_notification_stream_refuses_an_unknown_session(registry: Registry):
    client = await stateful_client(registry)
    try:
        resp = await client.get(
            "/mcp",
            headers={SESSION_HEADER: "no-such-session", "Accept": "text/event-stream"},
        )
        assert resp.status == 404
        assert (await resp.json())["error"]["code"] == -32001
    finally:
        await client.close()


async def test_a_request_without_the_header_is_served_as_before(registry: Registry):
    client = await stateful_client(registry)
    try:
        resp = await client.post(
            "/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {}}
        )
        assert resp.status == 200
        assert "error" not in await resp.json()
    finally:
        await client.close()


async def test_every_request_renews_the_session(registry: Registry):
    """A session lives while its client keeps talking, not for a fixed time
    after the handshake."""
    clock = Clock()
    registry.session_store = MemorySessionStore(clock=clock)
    registry.session_ttl_seconds = 10
    endpoint = Endpoint(registry)
    client = TestClient(TestServer(endpoint.app("/mcp")))
    await client.start_server()
    try:
        session_id = (await initialize(client, "2025-11-25")).headers[SESSION_HEADER]
        for step in (9, 18, 27):
            clock.now = step
            resp = await client.post(
                "/mcp",
                headers={SESSION_HEADER: session_id},
                json={"jsonrpc": "2.0", "id": 2, "method": "ping", "params": {}},
            )
            assert resp.status == 200, step
        clock.now = 37
        resp = await client.post(
            "/mcp",
            headers={SESSION_HEADER: session_id},
            json={"jsonrpc": "2.0", "id": 3, "method": "ping", "params": {}},
        )
        assert resp.status == 404
    finally:
        await client.close()


async def test_modern_revision_never_opens_a_session(registry: Registry):
    client = await stateful_client(registry)
    try:
        resp = await client.post(
            "/mcp",
            headers={"MCP-Protocol-Version": "2026-07-28", "Mcp-Method": "server/discover"},
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "server/discover",
                "params": {
                    "_meta": {
                        "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                        "io.modelcontextprotocol/clientCapabilities": {},
                    }
                },
            },
        )
        assert resp.status == 200
        assert SESSION_HEADER not in resp.headers
    finally:
        await client.close()


async def test_handle_session_round_trips(registry: Registry):
    registry.session_store = MemorySessionStore()
    ex = Exchange(registry, None, AdapterSet.default().by_version["2026-07-28"], None)
    assert ex.sessions is not None

    opened = await ex.sessions.open()
    await opened.set("project", "aiohttp-tiny-mcp")

    reached = await ex.sessions.use(opened.id)
    assert reached is not None
    assert reached.get("project") == "aiohttp-tiny-mcp"
    assert reached.id == opened.id


async def test_unknown_handle_is_none(registry: Registry):
    registry.session_store = MemorySessionStore()
    ex = Exchange(registry, None, AdapterSet.default().by_version["2026-07-28"], None)
    assert ex.sessions is not None
    assert await ex.sessions.use("forged-handle") is None


async def test_dropped_handle_stops_resolving(registry: Registry):
    registry.session_store = MemorySessionStore()
    ex = Exchange(registry, None, AdapterSet.default().by_version["2026-07-28"], None)
    assert ex.sessions is not None
    opened = await ex.sessions.open()
    await ex.sessions.drop(opened.id)
    assert await ex.sessions.use(opened.id) is None


async def test_two_handles_do_not_share_values(registry: Registry):
    registry.session_store = MemorySessionStore()
    ex = Exchange(registry, None, AdapterSet.default().by_version["2026-07-28"], None)
    assert ex.sessions is not None

    first = await ex.sessions.open()
    second = await ex.sessions.open()
    assert first.id != second.id

    await first.set("who", "first")
    await second.set("who", "second")
    assert (await ex.sessions.use(first.id)).get("who") == "first"
    assert (await ex.sessions.use(second.id)).get("who") == "second"


async def test_write_refuses_to_clobber_a_concurrent_change(registry: Registry):
    """Retry from fresh values so concurrent writes survive."""
    store = MemorySessionStore()
    registry.session_store = store
    ex = Exchange(registry, None, AdapterSet.default().by_version["2026-07-28"], None)
    assert ex.sessions is not None

    opened = await ex.sessions.open()
    other = await ex.sessions.use(opened.id)
    assert other is not None

    await opened.set("a", "1")
    await other.set("b", "2")  # still holds the pre-write version

    final = await ex.sessions.use(opened.id)
    assert final is not None
    assert final.get("a") == "1"
    assert final.get("b") == "2"


async def test_namespaces_separate_sessions(registry: Registry):
    registry.session_store = MemorySessionStore()

    namespace.set("tenant-a")
    ex = Exchange(registry, None, AdapterSet.default().by_version["2026-07-28"], None)
    assert ex.sessions is not None
    first = await ex.sessions.open()
    await first.set("who", "a")

    namespace.set("tenant-b")
    second = await ex.sessions.open()
    await second.set("who", "b")

    namespace.set("tenant-a")
    assert (await ex.sessions.use(first.id)).get("who") == "a"
    assert await ex.sessions.use(second.id) is None

    namespace.set("tenant-b")
    assert (await ex.sessions.use(second.id)).get("who") == "b"
    assert await ex.sessions.use(first.id) is None


async def test_a_leaked_handle_is_inert_elsewhere(registry: Registry):
    """Client-visible handles resolve only within their namespace."""
    registry.session_store = MemorySessionStore()
    namespace.set("owner")
    ex = Exchange(registry, None, AdapterSet.default().by_version["2026-07-28"], None)
    assert ex.sessions is not None
    session = await ex.sessions.open()
    await session.set("secret", "value")

    namespace.set("someone-else")
    assert await ex.sessions.use(session.id) is None

    namespace.set(None)
    assert await ex.sessions.use(session.id) is None


async def test_no_namespace_keeps_keys_bare(registry: Registry):
    store = MemorySessionStore()
    registry.session_store = store
    namespace.set(None)
    ex = Exchange(registry, None, AdapterSet.default().by_version["2026-07-28"], None)
    assert ex.sessions is not None
    session = await ex.sessions.open()
    assert session.id in store.records


async def test_a_namespace_holding_the_separator_cannot_collide(registry: Registry):
    """`a:b` with key `c` must not address the same row as `a` with `b:c`."""
    registry.session_store = MemorySessionStore()
    ex = Exchange(registry, None, AdapterSet.default().by_version["2026-07-28"], None)
    assert ex.sessions is not None

    namespace.set("a:b")
    tricky = await ex.sessions.open()
    await tricky.set("who", "colon")

    namespace.set("a")
    assert await ex.sessions.use(f"b:{tricky.id}") is None
