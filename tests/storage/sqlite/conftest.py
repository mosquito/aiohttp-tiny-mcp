from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from aiohttp_tiny_mcp.storage.sqlite import SqliteHub, SqliteSessionStore, SqliteStorage


@pytest.fixture
async def file(tmp_path) -> str:
    return str(tmp_path / "state.sqlite")


@pytest.fixture
async def storage(file) -> AsyncIterator[SqliteStorage]:
    held = SqliteStorage(file)
    try:
        yield held
    finally:
        await held.close()


@pytest.fixture
async def elsewhere(file) -> AsyncIterator[SqliteStorage]:
    """A second connection to the same file, standing in for another worker."""
    held = SqliteStorage(file)
    try:
        yield held
    finally:
        await held.close()


@pytest.fixture
def store(storage) -> SqliteSessionStore:
    return SqliteSessionStore(storage)


@pytest.fixture
def hub(storage) -> SqliteHub:
    return SqliteHub(storage, look_again=0.01)


@pytest.fixture
async def connections(tmp_path: Path) -> AsyncIterator[tuple[SqliteStorage, SqliteStorage]]:
    first = SqliteStorage(tmp_path / "state.sqlite3")
    second = SqliteStorage(tmp_path / "state.sqlite3")
    try:
        await first.open()
        await second.open()
        yield first, second
    finally:
        await first.close()
        await second.close()
