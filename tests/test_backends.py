"""Every store against every hub.

A store and a hub are separate contracts, and nothing says a deployment uses
one implementation for both: SQLite for sessions on one machine and Redis for
events across them is a reasonable thing to build. These pair each of the
four with each of the four and run the two exchanges that need both -- a
session opened on one worker and used on another, and a question answered
through the hub -- so an implementation that quietly assumed its partner is
caught here.

A pair naming a server skips where that server does not answer. See
tests/test_redis.py and tests/test_postgres.py.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
from aiohttp.test_utils import TestClient, TestServer
from pydantic import BaseModel

from aiohttp_tiny_mcp import (
    Client,
    Endpoint,
    Exchange,
    MemoryHub,
    MemorySessionStore,
    Registry,
    elicit,
    elicit_accept,
)
from aiohttp_tiny_mcp.protocol.selection import AdapterSet
from aiohttp_tiny_mcp.storage.hub import Hub
from aiohttp_tiny_mcp.storage.postgres import PostgresHub, PostgresSessionStore, PostgresStorage
from aiohttp_tiny_mcp.storage.redis import RedisHub, RedisSessionStore, RedisStorage
from aiohttp_tiny_mcp.storage.sessions import SessionStore
from aiohttp_tiny_mcp.storage.sqlite import SqliteHub, SqliteSessionStore, SqliteStorage

pytestmark = pytest.mark.asyncio

KINDS = ["memory", "sqlite", "redis", "postgres"]
MODERN = AdapterSet.default().by_version["2026-07-28"]
LEGACY = AdapterSet.default().by_version["2025-11-25"]


class Confirm(BaseModel):
    service: str


@pytest.fixture
async def backends(
    request, tmp_path, redis_url_or_none, postgres_url_or_none, unreachable
) -> AsyncIterator[tuple]:
    """One store and one hub, each of the kind this parameter names."""
    store_kind, hub_kind = request.param
    closing = []

    async def build(kind: str):
        if kind == "memory":
            return MemorySessionStore(), MemoryHub()
        if kind == "sqlite":
            storage = SqliteStorage(str(tmp_path / "state.sqlite"))
            closing.append(storage.close)
            return SqliteSessionStore(storage), SqliteHub(storage, look_again=0.01)
        if kind == "redis":
            if redis_url_or_none is None:
                unreachable("redis", "the pair needs one")
            pool = RedisStorage.from_url(redis_url_or_none)
            closing.append(pool.close)
            prefix = f"mcptest:{uuid.uuid4().hex[:12]}"
            return RedisSessionStore(pool, prefix=prefix), RedisHub(pool, prefix=prefix)
        if postgres_url_or_none is None:
            unreachable("postgres", "the pair needs one")
        pool = PostgresStorage.from_url(postgres_url_or_none)
        closing.append(pool.close)
        return PostgresSessionStore(pool), PostgresHub(pool)

    store = (await build(store_kind))[0]
    hub = (await build(hub_kind))[1]
    try:
        yield store, hub
    finally:
        for close in closing:
            await close()


PAIRS = [(store, hub) for store in KINDS for hub in KINDS]
PAIR_IDS = [f"{store}+{hub}" for store, hub in PAIRS]


def registry_on(store: SessionStore, hub: Hub) -> Registry:
    reg = Registry("pairs", "1.0", hub=hub, session_store=store, hub_poll_seconds=1.0)

    @reg.tool
    async def add(args: Confirm) -> str:
        """Echo, so a call has something to answer."""
        return f"hello {args.service}"

    @reg.tool
    async def confirm(args: Confirm, ex: Exchange) -> str:
        """Ask once, then act. The answer may arrive on another worker."""
        answer = await ex.ask("confirm", elicit(f"Deploy {args.service}?"))
        return f"deployed {args.service}" if answer.accepted else "stopped"

    return reg


@pytest.mark.parametrize("backends", PAIRS, ids=PAIR_IDS, indirect=True)
@pytest.mark.timeout(10)
async def test_a_session_opened_on_one_worker_is_used_on_another(backends):
    """The store carries it; the hub is not involved and must not have to be."""
    store, hub = backends
    registry = registry_on(store, hub)
    async with (
        TestClient(TestServer(Endpoint(registry).app())) as first,
        TestClient(TestServer(Endpoint(registry).app())) as second,
    ):
        async with Client(str(first.make_url("/mcp")), LEGACY) as opened:
            await opened.initialize()
            held = opened.session_id
        assert held is not None
        async with Client(str(second.make_url("/mcp")), LEGACY) as elsewhere:
            elsewhere.session_id = held
            done = await elsewhere.call_tool("add", {"service": "web"})
    assert done.content[0].text == "hello web"


@pytest.mark.parametrize("backends", PAIRS, ids=PAIR_IDS, indirect=True)
@pytest.mark.timeout(10)
async def test_a_question_asked_here_is_answered_there(backends):
    """The hub carries the answer; the store carries the round-trip state.
    This is the exchange that needs both at once."""
    store, hub = backends
    registry = registry_on(store, hub)

    async with (
        TestClient(TestServer(Endpoint(registry).app())) as first,
        TestClient(TestServer(Endpoint(registry).app())) as second,
    ):
        async with Client(str(first.make_url("/mcp")), MODERN) as asking:
            asked = await asking.call_tool("confirm", {"service": "web"})
        requests, state = MODERN.client_input_requests(asked)
        name = next(iter(requests))

        async with Client(str(second.make_url("/mcp")), MODERN) as answering:
            done = await answering.call_tool(
                "confirm",
                {"service": "web"},
                input_responses={name: elicit_accept({})},
                request_state=state,
            )
    assert done.content[0].text == "deployed web"
