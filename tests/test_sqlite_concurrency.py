from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import aiosqlite
import pytest

from aiohttp_tiny_mcp.storage.hub import START
from aiohttp_tiny_mcp.storage.sqlite import SqliteHub, SqliteSessionStore, SqliteStorage

pytestmark = pytest.mark.asyncio


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


@pytest.mark.parametrize("reader", ["session", "position", "events"])
@pytest.mark.parametrize(
    "writer", ["create", "save", "touch", "delete", "publish", "clear", "sweep"]
)
async def test_external_commit_cannot_promote_an_active_read_snapshot(
    connections: tuple[SqliteStorage, SqliteStorage],
    monkeypatch: pytest.MonkeyPatch,
    reader: str,
    writer: str,
) -> None:
    storage, elsewhere = connections
    sessions = SqliteSessionStore(storage)
    hub = SqliteHub(storage)
    await sessions.create("live", {}, ttl_seconds=60)
    await hub.publish("topic", {"n": 1})
    pending = asyncio.Event()
    resume = asyncio.Event()
    fetch = aiosqlite.Cursor.fetchall if reader == "events" else aiosqlite.Cursor.fetchone

    async def pause(cursor: aiosqlite.Cursor) -> Any:
        if asyncio.current_task() is reading:
            pending.set()
            await resume.wait()
        return await fetch(cursor)

    monkeypatch.setattr(aiosqlite.Cursor, fetch.__name__, pause)

    async def read() -> Any:
        if reader == "session":
            return await sessions.get("live")
        if reader == "position":
            return await hub.position("topic")
        return await hub.after("topic", START)

    async def write() -> Any:
        if writer == "create":
            return await sessions.create("new", {}, ttl_seconds=60)
        if writer == "save":
            return await sessions.save("live", {"n": 2}, expected_version=1, ttl_seconds=60)
        if writer == "touch":
            return await sessions.touch("live", ttl_seconds=60)
        if writer == "delete":
            return await sessions.delete("live")
        if writer == "publish":
            return await hub.publish("topic", {"n": 2})
        if writer == "clear":
            return await hub.delete("topic")
        return await storage.sweep()

    reading = asyncio.create_task(read())
    writing = None
    try:
        await asyncio.wait_for(pending.wait(), 2)
        await SqliteHub(elsewhere).publish("elsewhere", {"n": 3})
        writing = asyncio.create_task(write())
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(writing), 0.05)
    finally:
        resume.set()
        await reading
        if writing is not None:
            await writing
    assert await sessions.create("after", {}, ttl_seconds=60)
    assert await sessions.get("after") is not None


async def test_cancelled_reader_closes_cursor_before_next_write(
    connections: tuple[SqliteStorage, SqliteStorage], monkeypatch: pytest.MonkeyPatch
) -> None:
    storage, elsewhere = connections
    sessions = SqliteSessionStore(storage)
    await sessions.create("live", {}, ttl_seconds=60)
    pending = asyncio.Event()
    fetch = aiosqlite.Cursor.fetchone

    async def pause(cursor: aiosqlite.Cursor) -> Any:
        if asyncio.current_task() is reading:
            pending.set()
            await asyncio.Event().wait()
        return await fetch(cursor)

    monkeypatch.setattr(aiosqlite.Cursor, "fetchone", pause)
    reading = asyncio.create_task(sessions.get("live"))
    try:
        await asyncio.wait_for(pending.wait(), 2)
    finally:
        reading.cancel()
        with pytest.raises(asyncio.CancelledError):
            await reading
    await SqliteHub(elsewhere).publish("topic", {})
    assert await sessions.touch("live", ttl_seconds=60)


async def test_concurrent_startup_releases_pragma_cursors(tmp_path: Path) -> None:
    storages = [SqliteStorage(tmp_path / "state.sqlite3") for index in range(4)]
    try:
        await asyncio.gather(*(storage.open() for storage in storages))
        assert all(
            await asyncio.gather(
                *(
                    SqliteSessionStore(storage).create(str(index), {}, ttl_seconds=60)
                    for index, storage in enumerate(storages)
                )
            )
        )
    finally:
        await asyncio.gather(*(storage.close() for storage in storages))


async def test_startup_retries_only_busy_wal_transitions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    execute = aiosqlite.Connection.execute
    attempts = 0

    def busy_once(connection: aiosqlite.Connection, sql: str, parameters: Any = None) -> Any:
        nonlocal attempts
        if sql == "PRAGMA journal_mode=WAL":
            attempts += 1
            if attempts == 1:
                raise aiosqlite.OperationalError("database is locked")
        return execute(connection, sql, parameters)

    monkeypatch.setattr(aiosqlite.Connection, "execute", busy_once)
    storage = SqliteStorage(tmp_path / "state.sqlite3")
    try:
        await storage.open()
        assert attempts == 2
        assert await SqliteSessionStore(storage).create("live", {}, ttl_seconds=60)
    finally:
        await storage.close()


@pytest.mark.parametrize(
    ("message", "timeout"), [("database is locked", 0), ("disk I/O error", 5000)]
)
async def test_failed_startup_does_not_keep_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, message: str, timeout: int
) -> None:
    execute = aiosqlite.Connection.execute
    attempts = 0

    def unavailable(connection: aiosqlite.Connection, sql: str, parameters: Any = None) -> Any:
        nonlocal attempts
        if sql == "PRAGMA journal_mode=WAL":
            attempts += 1
            raise aiosqlite.OperationalError(message)
        return execute(connection, sql, parameters)

    monkeypatch.setattr(aiosqlite.Connection, "execute", unavailable)
    storage = SqliteStorage(tmp_path / "state.sqlite3", busy_timeout_ms=timeout)
    try:
        with pytest.raises(aiosqlite.OperationalError, match=message):
            await storage.open()
        assert attempts == 1
        assert storage.connection is None
        monkeypatch.setattr(aiosqlite.Connection, "execute", execute)
        await storage.open()
        assert await SqliteSessionStore(storage).create("live", {}, ttl_seconds=60)
    finally:
        await storage.close()
