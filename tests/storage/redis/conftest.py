from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest

from aiohttp_tiny_mcp.storage.redis import RedisHub, RedisSessionStore, RedisStorage
from tests.helpers import REDIS_URL, missing


@pytest.fixture
def prefix() -> str:
    """Keys of one test, so a shared server does not mix two runs together."""
    return f"mcptest:{uuid.uuid4().hex[:12]}"


@pytest.fixture
async def storage(redis_url) -> AsyncIterator[RedisStorage]:
    held = RedisStorage.from_url(redis_url)
    try:
        yield held
    finally:
        await held.close()


@pytest.fixture
async def elsewhere(redis_url) -> AsyncIterator[RedisStorage]:
    """A second connection to the same server, standing in for another worker."""
    held = RedisStorage.from_url(redis_url)
    try:
        yield held
    finally:
        await held.close()


@pytest.fixture
def store(storage, prefix) -> RedisSessionStore:
    return RedisSessionStore(storage, prefix=prefix)


@pytest.fixture
def hub(storage, prefix) -> RedisHub:
    return RedisHub(storage, prefix=prefix)


@pytest.fixture
def redis_url(redis_url_or_none) -> str:
    """A reachable Redis, or a skipped test.

    docker run -d -p 6379:6379 redis:7-alpine
    """
    if redis_url_or_none is None:
        missing("redis", REDIS_URL)
    return redis_url_or_none
