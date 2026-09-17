"""PostgreSQL `SessionStore` and `Hub` backends."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Mapping, Sequence
from functools import cached_property
from typing import Any

from .hub import START, Cursor, Event, Hub
from .sessions import SessionRecord, SessionStore

try:
    from psycopg import sql
    from psycopg_pool import AsyncConnectionPool
except ImportError as absent:  # pragma: no cover - depends on what is installed
    raise ImportError(
        'aiohttp_tiny_mcp.postgres needs psycopg. Install "aiohttp-tiny-mcp[postgres]".'
    ) from absent

log = logging.getLogger("aiohttp_tiny_mcp")

#: The key the sweep records itself under, in the state table.
SWEPT = "swept"

SCHEMA = """
CREATE TABLE IF NOT EXISTS {sessions} (
    id         TEXT PRIMARY KEY,
    data       JSONB NOT NULL,
    version    BIGINT NOT NULL DEFAULT 1,
    expires_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS {sessions_expiry} ON {sessions} (expires_at);

CREATE TABLE IF NOT EXISTS {events} (
    id         BIGSERIAL PRIMARY KEY,
    topic      TEXT NOT NULL,
    message    JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS {events_topic} ON {events} (topic, id);

CREATE TABLE IF NOT EXISTS {state} (
    key        TEXT PRIMARY KEY,
    value      JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


class PostgresStorage:
    """A pool, schema, and deployment maintenance state."""

    prefix: str = "mcp"

    event_ttl_seconds: int = 3600

    sweep_seconds: float = 60.0

    create_tables: bool = True

    def __init__(
        self,
        pool: AsyncConnectionPool,
        *,
        prefix: str | None = None,
        event_ttl_seconds: int | None = None,
        sweep_seconds: float | None = None,
        create_tables: bool | None = None,
        owned: bool = False,
    ) -> None:
        self.pool = pool
        self.owned = owned
        if prefix is not None:
            self.prefix = prefix
        if event_ttl_seconds is not None:
            self.event_ttl_seconds = event_ttl_seconds
        if sweep_seconds is not None:
            self.sweep_seconds = sweep_seconds
        if create_tables is not None:
            self.create_tables = create_tables
        self.opening = asyncio.Lock()
        self.prepared = False

    @classmethod
    def from_url(
        cls,
        conninfo: str,
        *,
        min_size: int = 4,
        max_size: int | None = None,
        **settings: Any,
    ) -> PostgresStorage:
        """Build an owned pool from a connection string."""
        return cls(
            AsyncConnectionPool(conninfo, open=False, min_size=min_size, max_size=max_size),
            owned=True,
            **settings,
        )

    @property
    def sessions_table(self) -> sql.Identifier:
        return sql.Identifier(f"{self.prefix}_sessions")

    @property
    def events_table(self) -> sql.Identifier:
        return sql.Identifier(f"{self.prefix}_events")

    @property
    def state_table(self) -> sql.Identifier:
        """Whatever this deployment has to remember between runs, by key."""
        return sql.Identifier(f"{self.prefix}_state")

    async def open(self) -> AsyncConnectionPool:
        """Open the pool and create the tables, once."""
        async with self.opening:
            if self.owned and self.pool.closed:
                await self.pool.open()
            if not self.prepared and self.create_tables:
                async with self.pool.connection() as connection:
                    await connection.execute(
                        sql.SQL(SCHEMA).format(
                            sessions=self.sessions_table,
                            events=self.events_table,
                            sessions_expiry=sql.Identifier(f"{self.prefix}_sessions_expiry"),
                            events_topic=sql.Identifier(f"{self.prefix}_events_topic"),
                            state=self.state_table,
                        )
                    )
            if not self.prepared:
                self.prepared = True
                log.debug("MCP state in the tables named %s_*", self.prefix)
            return self.pool

    async def close(self) -> None:
        if self.owned:
            await self.pool.close()
        self.prepared = False

    async def sweep(self) -> None:
        """Remove expired rows now. Use `sweep_if_due` on multiple workers."""
        pool = await self.open()
        async with pool.connection() as connection:
            await self.delete_expired(connection)

    async def delete_expired(self, connection: Any) -> None:
        await connection.execute(
            sql.Composed(
                [
                    sql.SQL("DELETE FROM "),
                    self.sessions_table,
                    sql.SQL(" WHERE expires_at <= now()"),
                ]
            )
        )
        await connection.execute(
            sql.Composed(
                [
                    sql.SQL("DELETE FROM "),
                    self.events_table,
                    sql.SQL(" WHERE created_at <= now() - make_interval(secs => %s)"),
                ]
            ),
            (self.event_ttl_seconds,),
        )

    async def sweep_if_due(self, *, every: float | None = None) -> bool:
        """Sweep once per interval across all workers; return whether this one did."""
        every = every if every is not None else self.sweep_seconds
        if not await self.due(every):
            return False
        pool = await self.open()
        async with pool.connection() as connection:
            async with connection.transaction():
                taken = await connection.execute(
                    "SELECT pg_try_advisory_xact_lock(hashtext(%s))", (f"{self.prefix}_sweep",)
                )
                row = await taken.fetchone()
                if row is None or not row[0]:
                    return False
                if not await self.due(every, connection):
                    return False
                await self.delete_expired(connection)
                await self.remember(SWEPT, {"seconds": every}, connection)
        return True

    async def due(self, every: float, connection: Any = None) -> bool:
        """Whether the last sweep was longer than `every` seconds ago."""
        if connection is None:
            pool = await self.open()
            async with pool.connection() as held:
                return await self.due(every, held)
        found = await connection.execute(
            sql.Composed(
                [
                    sql.SQL("SELECT updated_at <= now() - make_interval(secs => %s) FROM "),
                    self.state_table,
                    sql.SQL(" WHERE key = %s"),
                ]
            ),
            (every, SWEPT),
        )
        row = await found.fetchone()
        return True if row is None else bool(row[0])

    async def remember(self, key: str, value: Any, connection: Any = None) -> None:
        """Store one value under `key`, stamping `updated_at`."""
        if connection is None:
            pool = await self.open()
            async with pool.connection() as held:
                await self.remember(key, value, held)
                return
        await connection.execute(
            sql.Composed(
                [
                    sql.SQL("INSERT INTO "),
                    self.state_table,
                    sql.SQL("""
                    (key, value) VALUES (%s, %s)
                    ON CONFLICT (key) DO UPDATE
                        SET value = excluded.value, updated_at = now()
                    """),
                ]
            ),
            (key, json.dumps(value)),
        )

    async def recall(self, key: str) -> Any | None:
        """What was stored under `key`, or None."""
        pool = await self.open()
        async with pool.connection() as connection:
            found = await connection.execute(
                sql.Composed(
                    [sql.SQL("SELECT value FROM "), self.state_table, sql.SQL(" WHERE key = %s")]
                ),
                (key,),
            )
            row = await found.fetchone()
        return None if row is None else row[0]

    async def sweeping(self, *, every: float | None = None) -> None:
        """Sweep repeatedly; safe to run on every worker."""
        every = every if every is not None else self.sweep_seconds
        while True:
            await asyncio.sleep(every)
            try:
                await self.sweep_if_due(every=every)
            except Exception:  # noqa: BLE001 - a sweep must not end the loop
                log.exception("sweeping failed; trying again in %ss", every)

    async def cleanup_ctx(self, app: Any) -> AsyncIterator[None]:
        """Open and close the pool for an aiohttp application's lifetime."""
        await self.open()
        try:
            yield
        finally:
            await self.close()


class PostgresSessionStore(SessionStore):
    """`SessionStore` with version-checked writes, timed by the database."""

    def __init__(self, storage: PostgresStorage) -> None:
        self.storage = storage

    @cached_property
    def _query_create(self) -> sql.Composed:
        return sql.SQL("""
            INSERT INTO {table} (id, data, version, expires_at)
                VALUES (%s, %s, 1, now() + make_interval(secs => %s))
            ON CONFLICT (id) DO UPDATE
                SET data = excluded.data, version = 1, expires_at = excluded.expires_at
                WHERE {table}.expires_at <= now()
            """).format(table=self.storage.sessions_table)

    @cached_property
    def _query_get(self) -> sql.Composed:
        return sql.Composed(
            [
                sql.SQL("SELECT data, version FROM "),
                self.storage.sessions_table,
                sql.SQL(" WHERE id = %s AND expires_at > now()"),
            ]
        )

    @cached_property
    def _query_save(self) -> sql.Composed:
        return sql.Composed(
            [
                sql.SQL("UPDATE "),
                self.storage.sessions_table,
                sql.SQL("""
                SET data = %s,
                    version = version + 1,
                    expires_at = now() + make_interval(secs => %s)
                WHERE id = %s AND version = %s AND expires_at > now()
                """),
            ]
        )

    @cached_property
    def _query_delete(self) -> sql.Composed:
        return sql.Composed(
            [
                sql.SQL("DELETE FROM "),
                self.storage.sessions_table,
                sql.SQL(" WHERE id = %s"),
            ]
        )

    async def create(self, session_id: str, data: Mapping[str, Any], *, ttl_seconds: int) -> bool:
        pool = await self.storage.open()
        async with pool.connection() as connection:
            found = await connection.execute(
                self._query_create, (session_id, json.dumps(data), ttl_seconds)
            )
            return found.rowcount > 0

    async def get(self, session_id: str) -> SessionRecord | None:
        pool = await self.storage.open()
        async with pool.connection() as connection:
            found = await connection.execute(self._query_get, (session_id,))
            row = await found.fetchone()
        if row is None:
            return None
        return SessionRecord(data=row[0], version=row[1])

    async def save(
        self,
        session_id: str,
        data: Mapping[str, Any],
        *,
        expected_version: int,
        ttl_seconds: int,
    ) -> bool:
        pool = await self.storage.open()
        async with pool.connection() as connection:
            found = await connection.execute(
                self._query_save, (json.dumps(data), ttl_seconds, session_id, expected_version)
            )
            return found.rowcount > 0

    async def delete(self, session_id: str) -> None:
        pool = await self.storage.open()
        async with pool.connection() as connection:
            await connection.execute(self._query_delete, (session_id,))


class PostgresHub(Hub):
    """A cursor-based `Hub` backed by one PostgreSQL table."""

    look_again: float = 0.25

    def __init__(self, storage: PostgresStorage, *, look_again: float | None = None) -> None:
        self.storage = storage
        if look_again is not None:
            self.look_again = look_again

    @cached_property
    def _query_publish(self) -> sql.Composed:
        return sql.Composed(
            [
                sql.SQL("INSERT INTO "),
                self.storage.events_table,
                sql.SQL(" (topic, message) VALUES (%s, %s) RETURNING id"),
            ]
        )

    @cached_property
    def _query_position(self) -> sql.Composed:
        return sql.Composed(
            [
                sql.SQL("SELECT max(id) FROM "),
                self.storage.events_table,
                sql.SQL(" WHERE topic = %s"),
            ]
        )

    @cached_property
    def _query_after(self) -> sql.Composed:
        return sql.Composed(
            [
                sql.SQL("SELECT id, message FROM "),
                self.storage.events_table,
                sql.SQL(" WHERE topic = %s AND id > %s ORDER BY id"),
            ]
        )

    @cached_property
    def _query_delete(self) -> sql.Composed:
        return sql.Composed(
            [
                sql.SQL("DELETE FROM "),
                self.storage.events_table,
                sql.SQL(" WHERE topic = %s"),
            ]
        )

    async def publish(self, topic: str, message: Mapping[str, Any]) -> Cursor:
        pool = await self.storage.open()
        async with pool.connection() as connection:
            async with connection.transaction():
                await connection.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (topic,))
                inserted = await connection.execute(
                    self._query_publish, (topic, json.dumps(message))
                )
                row = await inserted.fetchone()
        assert row is not None
        return str(row[0])

    async def position(self, topic: str) -> Cursor:
        pool = await self.storage.open()
        async with pool.connection() as connection:
            found = await connection.execute(self._query_position, (topic,))
            row = await found.fetchone()
        return str(row[0]) if row is not None and row[0] is not None else START

    async def after(self, topic: str, cursor: Cursor) -> list[Event]:
        """Events after `cursor`. The row id is the event id."""
        pool = await self.storage.open()
        async with pool.connection() as connection:
            found = await connection.execute(
                self._query_after, (topic, int(cursor) if cursor else 0)
            )
            rows = await found.fetchall()
        return [Event(str(row[0]), row[1]) for row in rows]

    async def poll(self, topic: str, cursor: Cursor, *, timeout: float) -> Sequence[Event]:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            found = await self.after(topic, cursor)
            if found:
                return found
            left = deadline - loop.time()
            if left <= 0:
                return []
            await asyncio.sleep(min(self.look_again, left))

    async def delete(self, topic: str) -> None:
        pool = await self.storage.open()
        async with pool.connection() as connection:
            await connection.execute(self._query_delete, (topic,))


__all__ = [
    "SCHEMA",
    "SWEPT",
    "PostgresHub",
    "PostgresSessionStore",
    "PostgresStorage",
]
