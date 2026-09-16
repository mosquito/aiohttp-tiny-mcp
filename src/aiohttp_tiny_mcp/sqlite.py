"""SQLite `SessionStore` and `Hub` backends."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import suppress
from pathlib import Path
from typing import Any

from .hub import START, Cursor, Hub
from .sessions import SessionRecord, SessionStore

try:
    import aiosqlite
except ImportError as absent:  # pragma: no cover - depends on what is installed
    raise ImportError(
        'aiohttp_tiny_mcp.sqlite needs aiosqlite. Install "aiohttp-tiny-mcp[sqlite]".'
    ) from absent

log = logging.getLogger("aiohttp_tiny_mcp")

SCHEMA = """
CREATE TABLE IF NOT EXISTS mcp_sessions (
    id         TEXT PRIMARY KEY,
    data       TEXT NOT NULL,
    version    INTEGER NOT NULL,
    expires_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS mcp_sessions_expiry ON mcp_sessions (expires_at);

CREATE TABLE IF NOT EXISTS mcp_events (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    topic    TEXT NOT NULL,
    message  TEXT NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS mcp_events_topic ON mcp_events (topic, id);
"""

NOW = "CAST(strftime('%s', 'now') AS INTEGER)"


class SqliteStorage:
    """One SQLite connection shared by the store and hub."""

    busy_timeout_ms: int = 5000
    event_ttl_seconds: float = 3600.0
    sweep_seconds: float = 60.0

    def __init__(
        self,
        path: str | Path,
        *,
        busy_timeout_ms: int | None = None,
        event_ttl_seconds: int | None = None,
        sweep_seconds: float | None = None,
    ) -> None:
        self.path = str(path)
        if busy_timeout_ms is not None:
            self.busy_timeout_ms = busy_timeout_ms
        if event_ttl_seconds is not None:
            self.event_ttl_seconds = event_ttl_seconds
        if sweep_seconds is not None:
            self.sweep_seconds = sweep_seconds
        self.connection: aiosqlite.Connection | None = None
        self.opening = asyncio.Lock()

    async def open(self) -> aiosqlite.Connection:
        async with self.opening:
            if self.connection is None:
                connection = await aiosqlite.connect(self.path, isolation_level=None)
                await connection.execute("PRAGMA journal_mode=WAL")
                await connection.execute(f"PRAGMA busy_timeout={self.busy_timeout_ms}")
                await connection.executescript(SCHEMA)
                self.connection = connection
                log.debug("MCP state in %s", self.path)
            return self.connection

    async def close(self) -> None:
        async with self.opening:
            if self.connection is not None:
                await self.connection.close()
                self.connection = None

    async def sweep(self) -> None:
        """Remove what has expired. Safe to call at any time, from any worker."""
        connection = await self.open()
        await connection.execute(f"DELETE FROM mcp_sessions WHERE expires_at <= {NOW}")
        await connection.execute(
            f"DELETE FROM mcp_events WHERE created_at <= {NOW} - ?", (self.event_ttl_seconds,)
        )

    async def sweeping(self, *, every: float | None = None) -> None:
        """Sweep until cancelled. A failed sweep is logged and tried again."""
        every = every if every is not None else self.sweep_seconds
        while True:
            await asyncio.sleep(every)
            try:
                await self.sweep()
            except Exception:  # noqa: BLE001 -- a sweep must not end the loop
                log.exception("sweeping %s failed; trying again in %ss", self.path, every)

    async def cleanup_ctx(self, app: Any) -> AsyncIterator[None]:
        """Manage the connection and sweeper for an aiohttp app."""
        await self.open()
        sweeper = asyncio.create_task(self.sweeping())
        try:
            yield
        finally:
            sweeper.cancel()
            with suppress(asyncio.CancelledError):
                await sweeper
            await self.close()


class SqliteSessionStore(SessionStore):
    """`SessionStore` with version-checked writes, so two workers cannot lose each other's."""

    def __init__(self, storage: SqliteStorage) -> None:
        self.storage = storage

    async def create(self, session_id: str, data: Mapping[str, Any], *, ttl_seconds: int) -> bool:
        connection = await self.storage.open()
        cursor = await connection.execute(
            f"""
            INSERT INTO mcp_sessions (id, data, version, expires_at)
                VALUES (?, ?, 1, {NOW} + ?)
            ON CONFLICT(id) DO UPDATE
                SET data = excluded.data, version = 1, expires_at = excluded.expires_at
                WHERE mcp_sessions.expires_at <= {NOW}
            """,
            (session_id, json.dumps(data), ttl_seconds),
        )
        return cursor.rowcount > 0

    async def get(self, session_id: str) -> SessionRecord | None:
        connection = await self.storage.open()
        async with connection.execute(
            f"SELECT data, version FROM mcp_sessions WHERE id = ? AND expires_at > {NOW}",
            (session_id,),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        return SessionRecord(data=json.loads(row[0]), version=int(row[1]))

    async def save(
        self,
        session_id: str,
        data: Mapping[str, Any],
        *,
        expected_version: int,
        ttl_seconds: int,
    ) -> bool:
        connection = await self.storage.open()
        cursor = await connection.execute(
            f"""
            UPDATE mcp_sessions SET data = ?, version = version + 1, expires_at = {NOW} + ?
            WHERE id = ? AND version = ? AND expires_at > {NOW}
            """,
            (json.dumps(data), ttl_seconds, session_id, expected_version),
        )
        return cursor.rowcount > 0

    async def delete(self, session_id: str) -> None:
        connection = await self.storage.open()
        await connection.execute("DELETE FROM mcp_sessions WHERE id = ?", (session_id,))


class SqliteHub(Hub):
    """A cursor-based `Hub` backed by one SQLite table."""

    #: Polling interval. `poll` states a deadline; this is how it waits.
    look_again: float = 0.25

    def __init__(self, storage: SqliteStorage, *, look_again: float | None = None) -> None:
        self.storage = storage
        if look_again is not None:
            self.look_again = look_again

    async def publish(self, topic: str, message: Mapping[str, Any]) -> None:
        connection = await self.storage.open()
        await connection.execute(
            f"INSERT INTO mcp_events (topic, message, created_at) VALUES (?, ?, {NOW})",
            (topic, json.dumps(message)),
        )

    async def position(self, topic: str) -> Cursor:
        connection = await self.storage.open()
        async with connection.execute(
            "SELECT MAX(id) FROM mcp_events WHERE topic = ?", (topic,)
        ) as cursor:
            row = await cursor.fetchone()
        return str(row[0]) if row is not None and row[0] is not None else START

    async def after(self, topic: str, cursor: Cursor) -> tuple[list[Mapping[str, Any]], Cursor]:
        """Everything published after `cursor`, and where to continue from."""
        connection = await self.storage.open()
        async with connection.execute(
            "SELECT id, message FROM mcp_events WHERE topic = ? AND id > ? ORDER BY id",
            (topic, int(cursor) if cursor else 0),
        ) as rows:
            found = list(await rows.fetchall())
        if not found:
            return [], cursor
        return [json.loads(row[1]) for row in found], str(found[-1][0])

    async def poll(
        self, topic: str, cursor: Cursor, *, timeout: float
    ) -> tuple[Sequence[Mapping[str, Any]], Cursor]:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            messages, cursor = await self.after(topic, cursor)
            if messages:
                return messages, cursor
            left = deadline - loop.time()
            if left <= 0:
                return [], cursor
            await asyncio.sleep(min(self.look_again, left))

    async def delete(self, topic: str) -> None:
        connection = await self.storage.open()
        await connection.execute("DELETE FROM mcp_events WHERE topic = ?", (topic,))


__all__ = [
    "NOW",
    "SCHEMA",
    "SqliteHub",
    "SqliteSessionStore",
    "SqliteStorage",
]
