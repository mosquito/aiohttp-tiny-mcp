from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest

from aiohttp_tiny_mcp.storage.postgres import PostgresHub, PostgresSessionStore, PostgresStorage
from tests.helpers import POSTGRES_URL, missing


@pytest.fixture
def mine() -> str:
    """A name only this test uses, because the tables are shared."""
    return f"t{uuid.uuid4().hex[:12]}"


@pytest.fixture
async def storage(postgres_url) -> AsyncIterator[PostgresStorage]:
    held = PostgresStorage.from_url(postgres_url)
    try:
        yield held
    finally:
        await held.close()


@pytest.fixture
async def elsewhere(postgres_url) -> AsyncIterator[PostgresStorage]:
    """A second pool on the same server, standing in for another worker."""
    held = PostgresStorage.from_url(postgres_url)
    try:
        yield held
    finally:
        await held.close()


@pytest.fixture
def store(storage) -> PostgresSessionStore:
    return PostgresSessionStore(storage)


@pytest.fixture
def hub(storage) -> PostgresHub:
    return PostgresHub(storage)


@pytest.fixture
def postgres_url(postgres_url_or_none) -> str:
    """A reachable PostgreSQL, or a skipped test.

        docker run -d -p 5432:5432 -e POSTGRES_USER=mcp -e POSTGRES_PASSWORD=mcp \
            -e POSTGRES_DB=mcp postgres:17-alpine
    """
    if postgres_url_or_none is None:
        missing("postgres", POSTGRES_URL)
    return postgres_url_or_none
