from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack
from typing import Any

import psycopg
import pytest
import redis
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from pydantic import BaseModel, Field

from aiohttp_tiny_mcp import (  # noqa: E402
    Endpoint,
    Exchange,
    MemoryHub,
    MemorySessionStore,
    NeedInput,
    Registry,
    elicit,
    elicit_decline,
)
from aiohttp_tiny_mcp.models import CompleteParams
from aiohttp_tiny_mcp.protocol.selection import AdapterSet
from aiohttp_tiny_mcp.testing import serving


class Add(BaseModel):
    a: int
    b: int


class Sum(BaseModel):
    result: int


class Nothing(BaseModel):
    pass


class ItemRef(BaseModel):
    id: str


class Confirm(BaseModel):
    service: str


class Greet(BaseModel):
    language: str


VALUE_SCHEMA = {"type": "object", "properties": {"value": {"type": "string"}}}


class Routed(BaseModel):
    region: str = Field(json_schema_extra={"x-mcp-header": "Region"})


BACKENDS = ["memory", "sqlite", "redis", "postgres"]


@pytest.fixture(params=BACKENDS)
async def store_and_hub(
    request, tmp_path, redis_url_or_none, postgres_url_or_none
) -> AsyncIterator[tuple]:
    """A store and a hub of the kind this parameter names.

    Sessions have unguessable ids and topics carry the namespace, so runs on a
    shared server do not need to be told apart. Redis takes a prefix anyway,
    which costs nothing; PostgreSQL keeps its tables, because making a pair
    per test would cost a great deal.
    """
    kind = request.param
    if kind == "memory":
        yield MemorySessionStore(), MemoryHub()
        return
    if kind == "sqlite":
        from aiohttp_tiny_mcp.sqlite import SqliteHub, SqliteSessionStore, SqliteStorage

        storage = SqliteStorage(str(tmp_path / "state.sqlite"))
        try:
            yield SqliteSessionStore(storage), SqliteHub(storage, look_again=0.01)
        finally:
            await storage.close()
        return
    if kind == "redis":
        if redis_url_or_none is None:
            missing("redis", REDIS_URL)
        from aiohttp_tiny_mcp.redis import RedisHub, RedisSessionStore, RedisStorage

        pool = RedisStorage.from_url(redis_url_or_none, prefix=f"mcptest:{uuid.uuid4().hex[:12]}")
        try:
            yield RedisSessionStore(pool), RedisHub(pool)
        finally:
            await pool.close()
        return
    if postgres_url_or_none is None:
        missing("postgres", POSTGRES_URL)
    from aiohttp_tiny_mcp.postgres import PostgresHub, PostgresSessionStore, PostgresStorage

    pool = PostgresStorage.from_url(postgres_url_or_none)
    try:
        yield PostgresSessionStore(pool), PostgresHub(pool)
    finally:
        await pool.close()


@pytest.fixture
def registry(store_and_hub) -> Registry:
    store, hub = store_and_hub
    reg = Registry(
        "demo",
        "0.1.0",
        hub=hub,
        session_store=store,
        instructions="Test server.",
    )

    @reg.tool
    async def add(args: Add) -> Sum:
        """Add two integers."""
        return Sum(result=args.a + args.b)

    @reg.tool
    async def boom(args: Nothing) -> str:
        """Always raises."""
        raise RuntimeError("kaboom")

    @reg.tool
    async def confirm(args: Confirm, ex: Exchange) -> str:
        """MRTR round trip."""
        if (answer := ex.answers.get("confirm")) is not None:
            if answer.get("ok"):
                return f"deployed {ex.state['service']}"
            return "cancelled"
        if not ex.can_ask:
            return "cannot ask"
        raise NeedInput(
            {
                "confirm": elicit(
                    f"Deploy {args.service}?",
                    {"type": "object", "properties": {"ok": {"type": "boolean"}}},
                )
            },
            state={"service": args.service},
        )

    @reg.tool
    async def blind_ask(args: Nothing, ex: Exchange) -> str:
        """Exercise Dispatcher fallback without checking ex.can_ask."""
        if (answer := ex.answers.get("confirm")) is not None:
            return "ok" if answer.get("ok") else "declined"
        raise NeedInput(
            {
                "confirm": elicit(
                    "ok?", {"type": "object", "properties": {"ok": {"type": "boolean"}}}
                )
            }
        )

    @reg.tool
    async def gate(args: Nothing, ex: Exchange) -> str:
        """Confirmation without form fields."""
        if ex.answered("confirm"):
            return "accepted" if ex.accepted("confirm") else f"refused:{ex.action('confirm')}"
        if not ex.can_ask:
            return "cannot ask"
        raise NeedInput({"confirm": elicit("ok?", {"type": "object", "properties": {}})})

    @reg.tool
    async def gated(args: Nothing, ex: Exchange) -> str:
        """Single confirmation through `ask`."""
        confirm = await ex.ask("confirm", elicit("go ahead?"))
        if not confirm.accepted:
            return f"stopped at {confirm.action}"
        return "went ahead"

    @reg.tool
    async def twice(args: Nothing, ex: Exchange) -> str:
        """Two questions, one round trip each."""
        one = await ex.ask("one", elicit("first?", VALUE_SCHEMA))
        two = await ex.ask("two", elicit("second?", VALUE_SCHEMA))
        return f"{one['value']}+{two['value']}"

    @reg.tool
    async def defaulted(args: Nothing, ex: Exchange) -> str:
        """Answers for a client that cannot be asked."""
        confirm = await ex.ask("confirm", elicit("go ahead?"), default=elicit_decline())
        return "went ahead" if confirm.accepted else f"defaulted to {confirm.action}"

    @reg.tool
    async def listy(args: Nothing) -> list[int]:
        """Non-object structuredContent for revision compatibility checks."""
        return [1, 2, 3]

    @reg.tool(streaming=True)
    async def counter(args: Add, ex: Exchange) -> str:
        """Reports progress before the result."""
        for i in range(args.a, args.b):
            await ex.progress(i - args.a, args.b - args.a)
        return f"counted {args.b - args.a}"

    @reg.tool(streaming=True)
    async def noisy(args: Nothing, ex: Exchange) -> str:
        """Report at three severities to exercise client filtering."""
        await ex.log("debug", "looking")
        await ex.log("warning", "odd", logger="noisy")
        await ex.log("error", "bad")
        return "done"

    @reg.tool
    async def routed(args: Routed) -> str:
        """Echo a parameter mirrored through Mcp-Param-Region."""
        return args.region

    @reg.resource("config://app", mime_type="application/json")
    async def config(args: Nothing) -> dict:
        """Fixed resource."""
        return {"debug": False}

    @reg.resource("res://items/{id}", name="item")
    async def item(args: ItemRef) -> str:
        """Templated resource."""
        return f"item-{args.id}"

    @reg.resource("res://public", cache_ttl_ms=60_000, cache_scope="public")
    async def public_doc(args: Nothing) -> str:
        """Explicitly opted into public client-side caching."""
        return "public content"

    @reg.prompt
    async def greet(args: Greet) -> str:
        """Ask for a greeting."""
        return f"Hello, {args.language} speaker."

    @reg.completions
    async def complete(args: CompleteParams) -> list[str]:
        if args.argument.name == "language":
            return [x for x in ("python", "rust", "go") if x.startswith(args.argument.value)]
        return []

    return reg


@pytest.fixture
def endpoint(registry: Registry) -> Endpoint:
    return Endpoint(registry, adapters=AdapterSet.default())


@pytest.fixture
async def client(endpoint: Endpoint) -> AsyncIterator[TestClient]:
    app = endpoint.app("/mcp")
    server = TestServer(app)
    async with TestClient(server) as client:
        yield client


@pytest.fixture
async def real_endpoint() -> AsyncIterator[Any]:
    """Serve a test-provided registry on a real TCP socket.

    `testing.serving`, held open until the test ends, so a test may start
    more than one and forget about all of them.
    """
    async with AsyncExitStack() as running:

        async def start(registry: Registry, path: str = "/mcp") -> str:
            return await running.enter_async_context(serving(registry, path=path))

        yield start


@pytest.fixture
async def real_server_url(registry: Registry) -> AsyncIterator[str]:
    """Serve the shared fixture registry on a real TCP socket for SDK clients."""
    async with serving(registry) as url:
        yield url


@pytest.fixture
async def offered_over_sse(registry) -> AsyncIterator[str]:
    """The fixture registry on the transport 2024-11-05 speaks over HTTP."""
    from aiohttp_tiny_mcp import SseEndpoint

    app = web.Application()
    SseEndpoint(registry).setup(app)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    host, port = runner.addresses[0]
    try:
        yield f"http://{host}:{port}"
    finally:
        await runner.cleanup()


@pytest.fixture(params=[True, False], ids=["gzip", "plain"])
def enable_gzip(request) -> bool:
    """Run a stream test both compressed and not.

    The framing is the same either way, so anything that depends on the
    compressor is a defect rather than a mode.
    """
    return request.param


REQUIRED = os.environ.get("MCP_REQUIRE_BACKENDS") == "1"


def missing(what: str, url: str):
    """Skip, or fail where the backend was promised."""
    message = f"no {what} at {url}"
    if REQUIRED:
        pytest.fail(message)
    pytest.skip(message)


@pytest.fixture
def unreachable():
    """`missing`, for a test module that cannot import this one: at a full run
    the name `conftest` resolves to whichever one came first."""
    return missing


REDIS_URL = os.environ.get("REDIS_URL", "redis://127.0.0.1:6379")


@pytest.fixture(scope="session")
def redis_url_or_none() -> str | None:
    """A reachable Redis, or None. For tests that only partly need one."""
    try:
        redis.Redis.from_url(REDIS_URL, socket_connect_timeout=0.5).ping()
    except redis.RedisError:
        return None
    return REDIS_URL


@pytest.fixture
def redis_url(redis_url_or_none) -> str:
    """A reachable Redis, or a skipped test.

    docker run -d -p 6379:6379 redis:7-alpine
    """
    if redis_url_or_none is None:
        missing("redis", REDIS_URL)
    return redis_url_or_none


POSTGRES_URL = os.environ.get("POSTGRES_URL", "postgresql://mcp:mcp@127.0.0.1:5432/mcp")


@pytest.fixture(scope="session")
def postgres_url_or_none() -> str | None:
    """A reachable PostgreSQL, or None. For tests that only partly need one."""
    try:
        psycopg.connect(POSTGRES_URL, connect_timeout=2).close()
    except psycopg.Error:
        return None
    return POSTGRES_URL


@pytest.fixture
def postgres_url(postgres_url_or_none) -> str:
    """A reachable PostgreSQL, or a skipped test.

        docker run -d -p 5432:5432 -e POSTGRES_USER=mcp -e POSTGRES_PASSWORD=mcp \
            -e POSTGRES_DB=mcp postgres:17-alpine
    """
    if postgres_url_or_none is None:
        missing("postgres", POSTGRES_URL)
    return postgres_url_or_none
