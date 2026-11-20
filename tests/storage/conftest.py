from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest

from aiohttp_tiny_mcp import MemoryHub, MemorySessionStore
from aiohttp_tiny_mcp.storage.postgres import PostgresHub, PostgresSessionStore, PostgresStorage
from aiohttp_tiny_mcp.storage.redis import RedisHub, RedisSessionStore, RedisStorage
from aiohttp_tiny_mcp.storage.sqlite import SqliteHub, SqliteSessionStore, SqliteStorage
from tests.helpers import missing


@pytest.fixture
async def backends(
    request, tmp_path, redis_url_or_none, postgres_url_or_none
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
                missing("redis", "the pair needs one")
            pool = RedisStorage.from_url(redis_url_or_none)
            closing.append(pool.close)
            prefix = f"mcptest:{uuid.uuid4().hex[:12]}"
            return RedisSessionStore(pool, prefix=prefix), RedisHub(pool, prefix=prefix)
        if postgres_url_or_none is None:
            missing("postgres", "the pair needs one")
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
