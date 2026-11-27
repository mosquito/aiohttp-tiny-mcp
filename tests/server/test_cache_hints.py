"""Freshness fields on 2026-07-28: a declaration, a server-wide default, and precedence."""

from __future__ import annotations

from collections.abc import AsyncIterator
from types import MappingProxyType

import pytest
from aiohttp.test_utils import TestClient, TestServer
from pydantic import BaseModel

from aiohttp_tiny_mcp import Cache, Endpoint, Extension, MemoryHub, MemorySessionStore, Registry
from aiohttp_tiny_mcp.protocol.core import Call, ClientInfo, Operation
from aiohttp_tiny_mcp.protocol.models import CacheableResult, Params, ReadResourceResult
from aiohttp_tiny_mcp.protocol.selection import AdapterSet
from aiohttp_tiny_mcp.server.caching import CACHEABLE_METHODS, CacheHint
from aiohttp_tiny_mcp.testing import over_http

pytestmark = pytest.mark.asyncio

HINTS = MappingProxyType(
    {
        "tools/list": CacheHint(cache_ttl=30, scope="public"),
        "prompts/list": CacheHint(cache_ttl=20),
        "resources/list": CacheHint(cache_ttl=10, scope="public"),
        "resources/templates/list": CacheHint(cache_ttl=10, scope="public"),
        "server/discover": CacheHint(cache_ttl=5, scope="public"),
        "resources/read": CacheHint(cache_ttl=1, scope="public"),
    }
)


class Nothing(BaseModel):
    pass


class Ref(BaseModel):
    id: int


async def anything(args: Nothing) -> str:
    """Stands in for a handler."""
    return "ok"


async def item(args: Ref) -> str:
    """One item by id."""
    return f"item-{args.id}"


def registry_with(hints: dict[str, CacheHint] | None) -> Registry:
    registry = Registry(
        "cached",
        "1.0",
        hub=MemoryHub(),
        session_store=MemorySessionStore(),
        cache_hints=hints,
    )
    registry.tool(anything)
    registry.prompt(anything)
    registry.resource("config://app", anything)
    registry.resource("declared://app", anything, cache_ttl=60.125, cache_scope="public")
    registry.resource("items://{id}", item, name="item")
    return registry


@pytest.fixture
async def served(request) -> AsyncIterator[TestClient]:
    hints = getattr(request, "param", HINTS)
    app = Endpoint(registry_with(hints)).app("/mcp")
    async with TestClient(TestServer(app)) as client:
        yield client


async def result(client, method, params=None, *, name=None, version="2026-07-28"):
    headers = {"MCP-Protocol-Version": version, "Mcp-Method": method}
    if name is not None:
        headers["Mcp-Name"] = name
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": method,
        "params": {
            **(params or {}),
            "_meta": {
                "io.modelcontextprotocol/protocolVersion": version,
                "io.modelcontextprotocol/clientCapabilities": {},
            },
        },
    }
    response = await client.post("/mcp", headers=headers, json=body)
    answered = await response.json()
    assert "error" not in answered, answered
    return answered["result"]


@pytest.mark.parametrize("method", sorted(CACHEABLE_METHODS - {"resources/read"}))
async def test_a_default_reaches_every_cacheable_method(served, method):
    """A server states the freshness of a listing once, and every listing carries it."""
    answered = await result(served, method)
    assert answered["ttlMs"] == int((HINTS[method].cache_ttl or 0) * 1000)
    assert answered["cacheScope"] == (HINTS[method].scope or "private")


async def test_a_resource_declaration_wins_over_the_default(served):
    answered = await result(
        served, "resources/read", {"uri": "declared://app"}, name="declared://app"
    )
    assert answered["ttlMs"] == 60_125
    assert answered["cacheScope"] == "public"


async def test_a_resource_without_a_declaration_takes_the_default(served):
    answered = await result(served, "resources/read", {"uri": "config://app"}, name="config://app")
    assert answered["ttlMs"] == 1_000
    assert answered["cacheScope"] == "public"


@pytest.mark.parametrize("version", AdapterSet.default().versions)
async def test_a_read_can_override_the_ttl_without_changing_the_default(version):
    registry = registry_with({"resources/read": CacheHint(cache_ttl=1)})
    seen = []

    @registry.resource("dynamic://app")
    async def dynamic(args: Nothing, cache: Cache) -> str:
        seen.append(cache.ttl)
        cache.ttl += 0.5
        return "ok"

    async with over_http(registry, adapter=version) as client:
        for _ in range(2):
            answer = await client.read_resource("dynamic://app")
            assert answer.ttl_ms == (1_500 if version == "2026-07-28" else None)
    assert seen == [1, 1]


async def test_a_default_fills_only_the_field_nothing_else_states(served):
    """`prompts/list` declares a lifetime and no scope, so the scope falls back."""
    answered = await result(served, "prompts/list")
    assert answered["ttlMs"] == 20_000
    assert answered["cacheScope"] == "private"


@pytest.mark.parametrize("served", [None], indirect=True)
@pytest.mark.parametrize(
    "method", sorted(CACHEABLE_METHODS - {"resources/read", "resources/templates/list"})
)
async def test_without_a_default_nothing_is_kept(served, method):
    """Both fields are required, so the conservative pair is answered, not silence."""
    answered = await result(served, method)
    assert answered["ttlMs"] == 0
    assert answered["cacheScope"] == "private"


@pytest.mark.parametrize("version", ["2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"])
async def test_an_older_revision_carries_no_freshness_fields(served, version):
    """The fields exist only on 2026-07-28, and a default must not leak to the rest."""
    handshake = await served.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": version, "capabilities": {}},
        },
    )
    session = handshake.headers["Mcp-Session-Id"]
    response = await served.post(
        "/mcp",
        headers={"Mcp-Session-Id": session, "MCP-Protocol-Version": version},
        json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
    )
    answered = (await response.json())["result"]
    assert "ttlMs" not in answered
    assert "cacheScope" not in answered


async def test_a_hint_for_a_method_with_no_cacheable_result_is_refused():
    for method in ("tools/call", "prompts/get", "completion/complete", "subscriptions/listen"):
        with pytest.raises(ValueError, match="no cacheable result"):
            registry_with({method: CacheHint(cache_ttl=1)})


async def test_a_hint_that_is_not_a_cache_hint_is_refused():
    with pytest.raises(TypeError, match="must be a CacheHint"):
        registry_with({"tools/list": {"ttl_ms": 1}})  # type: ignore[dict-item]


@pytest.mark.parametrize("ttl", [-1, float("inf"), float("nan")])
async def test_an_invalid_lifetime_is_refused(ttl):
    with pytest.raises(ValueError, match="finite and non-negative"):
        CacheHint(cache_ttl=ttl)
    registry = registry_with(None)
    with pytest.raises(ValueError, match="finite and non-negative"):
        registry.resource("invalid://app", anything, cache_ttl=ttl)


async def test_a_scoped_resource_is_never_cached_publicly(auth):
    """The content depends on the caller, so a shared cache would serve it to another.

    A declaration already refuses `cache_scope="public"` on a scoped resource. This is
    the other way in: a server-wide default that says `public` for every read.
    """
    registry = Registry(
        "scoped",
        "1.0",
        hub=MemoryHub(),
        session_store=MemorySessionStore(),
        auth=auth,
        cache_hints={"resources/read": CacheHint(cache_ttl=9, scope="public")},
    )
    registry.resource("secret://app", anything, scopes={"files:read"})
    registry.resource("open://app", anything)

    adapter = AdapterSet.default().by_version["2026-07-28"]

    def read(uri):
        call = Call(
            operation=Operation.READ_RESOURCE,
            id=1,
            target=uri,
            arguments={},
            params=Params(),
            client=ClientInfo(),
        )
        return adapter.freshness(call, registry, ReadResourceResult(contents=[]))

    assert read("secret://app") == {"ttl_ms": 9_000, "cache_scope": "private"}
    assert read("open://app") == {"ttl_ms": 9_000, "cache_scope": "public"}


class Doubled(CacheableResult):
    doubled: int


@pytest.mark.parametrize("ttl_ms", [None, 7_000])
async def test_an_extension_result_takes_no_default(ttl_ms):
    """An extension answers under a method this revision does not name.

    There is no default for it, and asking for one used to raise `KeyError` on the way
    out. Its own fields are kept, and the conservative pair fills the rest.
    """
    registry = Registry(
        "extended",
        "1.0",
        hub=MemoryHub(),
        session_store=MemorySessionStore(),
        cache_hints={"tools/list": CacheHint(cache_ttl=30, scope="public")},
    )
    extension = Extension("example.org/arithmetic")

    @extension.method("arithmetic/double")
    async def double(args: Ref) -> Doubled:
        """Double a number."""
        return Doubled(doubled=args.id * 2, ttl_ms=ttl_ms)

    registry.extension(extension)
    async with TestClient(TestServer(Endpoint(registry).app("/mcp"))) as client:
        answered = await result(client, "arithmetic/double", {"id": 21})

    assert answered["doubled"] == 42
    assert answered["ttlMs"] == (ttl_ms or 0)
    assert answered["cacheScope"] == "private"
