from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from uuid import uuid4

import aiosqlite

DEFAULT_CHANNELS = (
    ("general", "Team announcements and general discussion."),
    ("random", "Informal conversation and ideas."),
    ("tasks", "Work coordination, progress, and results."),
)

MENTION = re.compile(r"(?<![\w.@+-])@([A-Za-z0-9][A-Za-z0-9_.@-]*)(?![\w.@-])")

SCHEMA = (
    "CREATE TABLE IF NOT EXISTS audit_events ("
    "id INTEGER PRIMARY KEY AUTOINCREMENT, "
    "created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')), "
    "org TEXT NOT NULL, user TEXT NOT NULL, tool TEXT NOT NULL, "
    "arguments TEXT NOT NULL, result TEXT NOT NULL, error TEXT);"
    "CREATE INDEX IF NOT EXISTS audit_org_user_id ON audit_events (org, user, id);"
    "CREATE INDEX IF NOT EXISTS audit_org_id ON audit_events (org, id);"
    "CREATE INDEX IF NOT EXISTS audit_org_user_tool_id ON audit_events (org, user, tool, id);"
    "CREATE INDEX IF NOT EXISTS audit_org_tool_id ON audit_events (org, tool, id);"
    "CREATE TABLE IF NOT EXISTS channels ("
    "org TEXT NOT NULL, name TEXT NOT NULL, description TEXT NOT NULL, "
    "created_by TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT "
    "(strftime('%Y-%m-%dT%H:%M:%fZ', 'now')), PRIMARY KEY (org, name));"
    "CREATE TABLE IF NOT EXISTS messages ("
    "id INTEGER PRIMARY KEY AUTOINCREMENT, org TEXT NOT NULL, channel TEXT NOT NULL, "
    "user TEXT NOT NULL, text TEXT NOT NULL, parent_id INTEGER, "
    "created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')), "
    "UNIQUE (org, channel, id), "
    "FOREIGN KEY (org, channel) REFERENCES channels (org, name), "
    "FOREIGN KEY (org, channel, parent_id) REFERENCES messages (org, channel, id));"
    "CREATE TABLE IF NOT EXISTS subscriptions ("
    "org TEXT NOT NULL, user TEXT NOT NULL, channel TEXT NOT NULL, "
    "last_read_id INTEGER NOT NULL DEFAULT 0, "
    "PRIMARY KEY (org, user, channel), "
    "FOREIGN KEY (org, channel) REFERENCES channels (org, name));"
    "CREATE INDEX IF NOT EXISTS subscriptions_channel ON subscriptions (org, channel, user);"
    "CREATE TABLE IF NOT EXISTS mentions ("
    "org TEXT NOT NULL, user TEXT NOT NULL, channel TEXT NOT NULL, message_id INTEGER NOT NULL, "
    "is_read INTEGER NOT NULL DEFAULT 0 CHECK (is_read IN (0, 1)), "
    "PRIMARY KEY (org, user, message_id), "
    "FOREIGN KEY (org, channel, message_id) REFERENCES messages (org, channel, id));"
    "CREATE INDEX IF NOT EXISTS mentions_channel "
    "ON mentions (org, user, channel, is_read, message_id);"
    "CREATE INDEX IF NOT EXISTS mentions_unread ON mentions (org, user, is_read, message_id);"
    "CREATE INDEX IF NOT EXISTS mentions_message ON mentions (org, channel, message_id, user);"
    "CREATE TABLE IF NOT EXISTS direct_conversations ("
    "org TEXT NOT NULL, channel TEXT NOT NULL, user_a TEXT NOT NULL, user_b TEXT NOT NULL, "
    "PRIMARY KEY (org, channel), UNIQUE (org, user_a, user_b), CHECK (user_a < user_b), "
    "FOREIGN KEY (org, channel) REFERENCES channels (org, name));"
    "CREATE INDEX IF NOT EXISTS messages_org_id ON messages (org, id);"
    "CREATE INDEX IF NOT EXISTS messages_thread ON messages (org, parent_id, id);"
)

FTS_SCHEMA = (
    "CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5("
    "text, content='messages', content_rowid='id', tokenize='unicode61 remove_diacritics 2')",
    "CREATE TRIGGER IF NOT EXISTS messages_fts_insert AFTER INSERT ON messages BEGIN "
    "INSERT INTO messages_fts(rowid, text) VALUES (new.id, new.text); END",
    "CREATE TRIGGER IF NOT EXISTS messages_fts_delete AFTER DELETE ON messages BEGIN "
    "INSERT INTO messages_fts(messages_fts, rowid, text) VALUES ('delete', old.id, old.text); END",
    "CREATE TRIGGER IF NOT EXISTS messages_fts_update AFTER UPDATE OF text ON messages BEGIN "
    "INSERT INTO messages_fts(messages_fts, rowid, text) VALUES ('delete', old.id, old.text); "
    "INSERT INTO messages_fts(rowid, text) VALUES (new.id, new.text); END",
)


class Store:
    """Persist organization-scoped chat data on one serialized connection."""

    def __init__(self, path: str | Path) -> None:
        self.path = path
        self.connection: aiosqlite.Connection | None = None
        self.lock = asyncio.Lock()

    async def open(self) -> None:
        async with self.lock:
            if self.connection is not None:
                return
            connection = await aiosqlite.connect(self.path, isolation_level=None)
            try:
                connection.row_factory = aiosqlite.Row
                for pragma in (
                    "PRAGMA busy_timeout = 5000",
                    "PRAGMA foreign_keys = ON",
                ):
                    async with connection.execute(pragma) as cursor:
                        await cursor.fetchall()
                deadline = asyncio.get_running_loop().time() + 5
                while True:
                    try:
                        async with connection.execute("PRAGMA journal_mode = WAL") as cursor:
                            await cursor.fetchall()
                        break
                    except aiosqlite.OperationalError as error:
                        code = getattr(error, "sqlite_errorcode", None)
                        busy = (
                            (code & 255) == 5
                            if code is not None
                            else str(error) == "database is locked"
                        )
                        if not busy or asyncio.get_running_loop().time() >= deadline:
                            raise
                        # Concurrent WAL transitions can return BUSY without invoking busy_timeout.
                        await asyncio.sleep(0.01)
                async with connection.executescript(SCHEMA):
                    pass
                async with connection.execute("BEGIN IMMEDIATE"):
                    pass
                existing = await self.rows(
                    connection,
                    "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
                    ("messages_fts",),
                )
                for statement in FTS_SCHEMA:
                    async with connection.execute(statement):
                        pass
                if not existing:
                    async with connection.execute(
                        "INSERT INTO messages_fts(messages_fts) VALUES ('rebuild')"
                    ):
                        pass
                await connection.commit()
            except BaseException:
                await connection.rollback()
                await connection.close()
                raise
            self.connection = connection

    async def close(self) -> None:
        async with self.lock:
            if self.connection is not None:
                await self.connection.close()
                self.connection = None

    async def ensure_organization(self, org: str) -> None:
        """Create missing default channels without changing existing channels or subscriptions."""
        async with self.transaction() as connection:
            await connection.executemany(
                "INSERT INTO channels (org, name, description, created_by) VALUES (?, ?, ?, ?) "
                "ON CONFLICT (org, name) DO NOTHING",
                [(org, name, description, "system") for name, description in DEFAULT_CHANNELS],
            )

    async def record_event(
        self,
        org: str,
        user: str,
        tool: str,
        arguments: dict[str, Any],
        result: Any,
        error: str | None,
    ) -> None:
        async with self.transaction() as connection:
            async with connection.execute(
                "INSERT INTO audit_events (org, user, tool, arguments, result, error) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (org, user, tool, json.dumps(arguments), json.dumps(result), error),
            ):
                pass

    async def latest_event(
        self, org: str, user: str | None, *, tools: list[str] | tuple[str, ...] | None = None
    ) -> int:
        tool_filter = "" if tools is None else f" AND tool IN ({','.join('?' for name in tools)})"
        user_filter = "" if user is None else " AND user = ?"
        async with self.lock:
            connection = self.connection
            if connection is None:
                raise RuntimeError("The chat store is not open.")
            rows = await self.rows(
                connection,
                "SELECT coalesce(max(id), 0) AS id FROM audit_events WHERE org = ?"
                + user_filter
                + tool_filter,
                (org, *((user,) if user is not None else ()), *(tools or ())),
            )
        return rows[0]["id"]

    async def observe(
        self,
        org: str,
        user: str | None = None,
        before: int | None = None,
        limit: int = 50,
        *,
        tools: list[str] | tuple[str, ...] | None = None,
    ) -> dict[str, Any]:
        if not 1 <= limit <= 100 or (before is not None and before < 1):
            raise ValueError("Use a positive before ID and a limit between 1 and 100.")
        tool_filter = "" if tools is None else f" AND a.tool IN ({','.join('?' for name in tools)})"
        user_filter = "" if user is None else " AND a.user = ?"
        before_filter = "" if before is None else " AND a.id < ?"
        async with self.lock:
            connection = self.connection
            if connection is None:
                raise RuntimeError("The chat store is not open.")
            events = await self.rows(
                connection,
                "SELECT a.*, d.channel AS dm_channel, "
                "CASE WHEN d.user_a = a.user THEN d.user_b ELSE d.user_a END AS dm_peer "
                "FROM audit_events a LEFT JOIN direct_conversations d ON d.org = a.org "
                "AND a.user IN (d.user_a, d.user_b) AND d.channel = coalesce("
                "json_extract(a.arguments, '$.channel'), "
                "json_extract(a.result, '$.structuredContent.channel')) "
                "WHERE a.org = ?"
                + user_filter
                + before_filter
                + tool_filter
                + " ORDER BY a.id DESC LIMIT ?",
                (
                    org,
                    *((user,) if user is not None else ()),
                    *((before,) if before is not None else ()),
                    *(tools or ()),
                    limit + 1,
                ),
            )
            users = await self.rows(
                connection,
                "SELECT DISTINCT user FROM audit_events WHERE org = ? ORDER BY user",
                (org,),
            )
            orgs = await self.rows(
                connection, "SELECT DISTINCT org FROM audit_events ORDER BY org", ()
            )
        has_more = len(events) > limit
        events = events[:limit]
        for event in events:
            event["arguments"] = json.loads(event["arguments"])
            event["result"] = json.loads(event["result"])
            channel = event.pop("dm_channel")
            peer = event.pop("dm_peer")
            if channel is not None:
                event["conversation"] = {"kind": "dm", "peer": peer, "channel": channel}
        return {
            "events": events,
            "next_before": events[-1]["id"] if has_more else None,
            "has_more": has_more,
            "users": [row["user"] for row in users],
            "orgs": [row["org"] for row in orgs],
        }

    @asynccontextmanager
    async def transaction(self) -> AsyncGenerator[aiosqlite.Connection, None]:
        async with self.lock:
            connection = self.connection
            if connection is None:
                raise RuntimeError("The chat store is not open.")
            try:
                await connection.execute("BEGIN IMMEDIATE")
                yield connection
                await connection.commit()
            except BaseException:
                await connection.rollback()
                raise

    @staticmethod
    async def rows(
        connection: aiosqlite.Connection, sql: str, parameters: tuple[Any, ...]
    ) -> list[dict[str, Any]]:
        async with connection.execute(sql, parameters) as cursor:
            return [dict(row) for row in await cursor.fetchall()]

    @staticmethod
    def check_page(after: int, limit: int) -> None:
        if after < 0 or not 1 <= limit <= 100:
            raise ValueError("Use after >= 0 and a limit between 1 and 100.")

    @staticmethod
    def mentioned_users(text: str) -> set[str]:
        """Extract complete case-sensitive names without matching email fragments."""
        return {match[1] for match in MENTION.finditer(text) if len(match[1]) <= 64}

    async def require_channel(
        self, connection: aiosqlite.Connection, org: str, channel: str, user: str | None = None
    ) -> dict[str, Any]:
        rows = await self.rows(
            connection,
            "SELECT c.*, CASE WHEN d.channel IS NULL THEN 'channel' ELSE 'dm' END AS kind "
            "FROM channels c LEFT JOIN direct_conversations d "
            "ON d.org = c.org AND d.channel = c.name "
            "WHERE c.org = ? AND c.name = ? AND (d.channel IS NULL OR ? IN (d.user_a, d.user_b))",
            (org, channel, user),
        )
        if not rows:
            raise ValueError("Channel not found.")
        return rows[0]

    async def require_subscription(
        self, connection: aiosqlite.Connection, org: str, user: str, channel: str
    ) -> dict[str, Any]:
        await self.require_channel(connection, org, channel, user)
        rows = await self.rows(
            connection,
            "SELECT * FROM subscriptions WHERE org = ? AND user = ? AND channel = ?",
            (org, user, channel),
        )
        if not rows:
            raise ValueError("Subscribe to the channel first.")
        return rows[0]

    async def require_message(
        self, connection: aiosqlite.Connection, org: str, message_id: int, user: str | None = None
    ) -> dict[str, Any]:
        rows = await self.rows(
            connection,
            "SELECT m.* FROM messages m LEFT JOIN direct_conversations d "
            "ON d.org = m.org AND d.channel = m.channel WHERE m.org = ? AND m.id = ? "
            "AND (d.channel IS NULL OR ? IN (d.user_a, d.user_b))",
            (org, message_id, user),
        )
        if not rows:
            raise ValueError("Message not found.")
        return rows[0]

    async def create_channel(
        self, org: str, user: str, name: str, description: str = ""
    ) -> dict[str, Any]:
        async with self.transaction() as connection:
            existing = await self.rows(
                connection, "SELECT name FROM channels WHERE org = ? AND name = ?", (org, name)
            )
            if existing:
                raise ValueError("Channel already exists.")
            await connection.execute(
                "INSERT INTO channels (org, name, description, created_by) VALUES (?, ?, ?, ?)",
                (org, name, description, user),
            )
            await connection.execute(
                "INSERT INTO subscriptions (org, user, channel) VALUES (?, ?, ?)", (org, user, name)
            )
            return await self.require_channel(connection, org, name)

    async def list_channels(self, org: str, user: str) -> list[dict[str, Any]]:
        async with self.transaction() as connection:
            rows = await self.rows(
                connection,
                "SELECT c.*, s.channel IS NOT NULL AS subscribed, "
                "COALESCE(s.last_read_id, 0) AS last_read_id, "
                "((SELECT count(*) FROM messages m WHERE m.org = c.org AND m.channel = c.name "
                "AND m.id > s.last_read_id AND m.user != ?) + "
                "(SELECT count(*) FROM mentions n WHERE n.org = c.org AND n.channel = c.name "
                "AND n.user = ? AND n.is_read = 0 "
                "AND (s.last_read_id IS NULL OR n.message_id <= s.last_read_id))) AS unread "
                "FROM channels c LEFT JOIN subscriptions s "
                "ON s.org = c.org AND s.channel = c.name AND s.user = ? "
                "WHERE c.org = ? AND NOT EXISTS (SELECT 1 FROM direct_conversations d "
                "WHERE d.org = c.org AND d.channel = c.name) ORDER BY c.name",
                (user, user, user, org),
            )
            for row in rows:
                row["subscribed"] = bool(row["subscribed"])
            return rows

    async def subscribe(self, org: str, user: str, channel: str) -> dict[str, Any]:
        async with self.transaction() as connection:
            found = await self.require_channel(connection, org, channel, user)
            if found["kind"] == "dm":
                raise ValueError("Direct message participants are subscribed automatically.")
            await connection.execute(
                "INSERT INTO subscriptions (org, user, channel, last_read_id) "
                "SELECT ?, ?, ?, COALESCE(MAX(id), 0) FROM messages "
                "WHERE org = ? AND channel = ? ON CONFLICT (org, user, channel) DO NOTHING",
                (org, user, channel, org, channel),
            )
            return await self.require_subscription(connection, org, user, channel)

    async def unsubscribe(self, org: str, user: str, channel: str) -> dict[str, Any]:
        async with self.transaction() as connection:
            found = await self.require_channel(connection, org, channel, user)
            if found["kind"] == "dm":
                raise ValueError("Direct message participants cannot leave the conversation.")
            await connection.execute(
                "DELETE FROM subscriptions WHERE org = ? AND user = ? AND channel = ?",
                (org, user, channel),
            )
            return {"channel": channel, "subscribed": False}

    async def list_subscriptions(self, org: str, user: str) -> list[dict[str, Any]]:
        return [channel for channel in await self.list_channels(org, user) if channel["subscribed"]]

    async def direct_rows(
        self, connection: aiosqlite.Connection, org: str, user: str, channel: str | None = None
    ) -> list[dict[str, Any]]:
        rows = await self.rows(
            connection,
            "SELECT c.*, 'dm' AS kind, "
            "CASE WHEN d.user_a = ? THEN d.user_b ELSE d.user_a END AS peer, "
            "s.last_read_id, (SELECT count(*) FROM messages m WHERE m.org = c.org "
            "AND m.channel = c.name AND m.id > s.last_read_id AND m.user != ?) AS unread "
            "FROM direct_conversations d JOIN channels c ON c.org = d.org AND c.name = d.channel "
            "JOIN subscriptions s ON s.org = c.org AND s.channel = c.name AND s.user = ? "
            "WHERE d.org = ? AND ? IN (d.user_a, d.user_b) AND (? IS NULL OR d.channel = ?) "
            "ORDER BY peer, c.name",
            (user, user, user, org, user, channel, channel),
        )
        for row in rows:
            row["subscribed"] = True
        return rows

    async def open_dm(self, org: str, user: str, peer: str) -> dict[str, Any]:
        """Open the unique private conversation for two users in one organization."""
        for name in (user, peer):
            if not isinstance(name, str) or not re.fullmatch(
                r"[a-zA-Z0-9][a-zA-Z0-9_.@-]{0,63}", name
            ):
                raise ValueError("User must contain 1–64 letters, digits, or . _ @ -.")
        if user == peer:
            raise ValueError("Choose another user for a direct message.")
        first, second = sorted((user, peer))
        async with self.transaction() as connection:
            existing = await self.rows(
                connection,
                "SELECT channel FROM direct_conversations "
                "WHERE org = ? AND user_a = ? AND user_b = ?",
                (org, first, second),
            )
            if existing:
                channel = existing[0]["channel"]
            else:
                channel = f"dm-{uuid4().hex}"
                await connection.execute(
                    "INSERT INTO channels (org, name, description, created_by) VALUES (?, ?, ?, ?)",
                    (org, channel, "Private direct conversation.", user),
                )
                await connection.execute(
                    "INSERT INTO direct_conversations (org, channel, user_a, user_b) "
                    "VALUES (?, ?, ?, ?)",
                    (org, channel, first, second),
                )
            await connection.executemany(
                "INSERT INTO subscriptions (org, user, channel) VALUES (?, ?, ?) "
                "ON CONFLICT (org, user, channel) DO NOTHING",
                [(org, participant, channel) for participant in (first, second)],
            )
            return (await self.direct_rows(connection, org, user, channel))[0]

    async def list_dms(self, org: str, user: str) -> list[dict[str, Any]]:
        async with self.transaction() as connection:
            return await self.direct_rows(connection, org, user)

    async def send_dm(
        self, org: str, user: str, peer: str, text: str, parent_id: int | None = None
    ) -> dict[str, Any]:
        conversation = await self.open_dm(org, user, peer)
        return await self.post_message(org, user, conversation["name"], text, parent_id)

    async def is_direct(self, org: str, user: str, channel: str) -> bool:
        async with self.transaction() as connection:
            found = await self.require_channel(connection, org, channel, user)
            return found["kind"] == "dm"

    async def notification_recipients(
        self, org: str, user: str, channel: str, message_id: int | None = None
    ) -> dict[str, Any]:
        """Return authorized recipients after checking the caller's channel access."""
        async with self.transaction() as connection:
            found = await self.require_channel(connection, org, channel, user)
            if found["kind"] == "dm":
                rows = await self.rows(
                    connection,
                    "SELECT user_a, user_b FROM direct_conversations WHERE org = ? AND channel = ?",
                    (org, channel),
                )
                users = [rows[0]["user_a"], rows[0]["user_b"]]
            else:
                rows = await self.rows(
                    connection,
                    "SELECT user FROM subscriptions WHERE org = ? AND channel = ? "
                    "UNION SELECT user FROM mentions WHERE org = ? AND channel = ? "
                    "AND message_id = ? ORDER BY user",
                    (org, channel, org, channel, message_id),
                )
                users = [row["user"] for row in rows]
            return {"kind": found["kind"], "users": users}

    async def post_message(
        self, org: str, user: str, channel: str, text: str, parent_id: int | None = None
    ) -> dict[str, Any]:
        async with self.transaction() as connection:
            await self.require_subscription(connection, org, user, channel)
            if parent_id is not None:
                parent = await self.require_message(connection, org, parent_id, user)
                if parent["channel"] != channel or parent["parent_id"] is not None:
                    raise ValueError("Reply to a root message in this channel.")
            async with connection.execute(
                "INSERT INTO messages (org, user, channel, text, parent_id) VALUES (?, ?, ?, ?, ?)",
                (org, user, channel, text, parent_id),
            ) as cursor:
                message_id = cursor.lastrowid
            assert message_id is not None
            found = await self.require_channel(connection, org, channel, user)
            if found["kind"] != "dm":
                await connection.executemany(
                    "INSERT INTO mentions (org, user, channel, message_id) VALUES (?, ?, ?, ?)",
                    [
                        (org, recipient, channel, message_id)
                        for recipient in sorted(self.mentioned_users(text) - {user})
                    ],
                )
            return await self.require_message(connection, org, message_id, user)

    async def history(
        self, org: str, channel: str, after: int = 0, limit: int = 50, *, user: str | None = None
    ) -> list[dict[str, Any]]:
        self.check_page(after, limit)
        async with self.transaction() as connection:
            await self.require_channel(connection, org, channel, user)
            return await self.rows(
                connection,
                "SELECT * FROM messages WHERE org = ? AND channel = ? AND id > ? "
                "ORDER BY id LIMIT ?",
                (org, channel, after, limit),
            )

    async def thread(
        self, org: str, message_id: int, after: int = 0, limit: int = 50, *, user: str | None = None
    ) -> list[dict[str, Any]]:
        self.check_page(after, limit)
        async with self.transaction() as connection:
            message = await self.require_message(connection, org, message_id, user)
            if message["parent_id"] is not None:
                raise ValueError("Use the root message ID to read a thread.")
            return await self.rows(
                connection,
                "SELECT * FROM messages WHERE org = ? AND id = ? AND id > ? "
                "UNION ALL SELECT * FROM messages WHERE org = ? AND parent_id = ? "
                "AND id > ? ORDER BY id LIMIT ?",
                (org, message_id, after, org, message_id, after, limit),
            )

    async def inbox(
        self, org: str, user: str, after: int = 0, limit: int = 50
    ) -> list[dict[str, Any]]:
        self.check_page(after, limit)
        async with self.transaction() as connection:
            return await self.rows(
                connection,
                "WITH members AS ("
                "SELECT m.id FROM subscriptions s LEFT JOIN direct_conversations d "
                "ON d.org = s.org AND d.channel = s.channel CROSS JOIN messages m "
                "ON m.id IN (SELECT id FROM messages WHERE org = s.org AND channel = s.channel "
                "AND id > max(s.last_read_id, ?) AND user != ? ORDER BY id LIMIT ?) "
                "WHERE s.org = ? AND s.user = ? "
                "AND (d.channel IS NULL OR ? IN (d.user_a, d.user_b)) "
                "ORDER BY m.id LIMIT ?), addressed AS ("
                "SELECT message_id AS id FROM mentions WHERE org = ? AND user = ? "
                "AND is_read = 0 AND message_id > ? ORDER BY message_id LIMIT ?), unread AS ("
                "SELECT id FROM members UNION SELECT id FROM addressed) "
                "SELECT m.* FROM unread u CROSS JOIN messages m ON m.id = u.id "
                "LEFT JOIN direct_conversations d ON d.org = m.org AND d.channel = m.channel "
                "WHERE m.org = ? AND m.user != ? "
                "AND (d.channel IS NULL OR ? IN (d.user_a, d.user_b)) "
                "ORDER BY m.id LIMIT ?",
                (
                    after,
                    user,
                    limit,
                    org,
                    user,
                    user,
                    limit,
                    org,
                    user,
                    after,
                    limit,
                    org,
                    user,
                    user,
                    limit,
                ),
            )

    async def mark_read(self, org: str, user: str, channel: str, message_id: int) -> dict[str, Any]:
        async with self.transaction() as connection:
            await self.require_channel(connection, org, channel, user)
            message = await self.require_message(connection, org, message_id, user)
            if message["channel"] != channel:
                raise ValueError("Message not found in this channel.")
            subscriptions = await self.rows(
                connection,
                "SELECT * FROM subscriptions WHERE org = ? AND user = ? AND channel = ?",
                (org, user, channel),
            )
            if not subscriptions:
                mentions = await self.rows(
                    connection,
                    "SELECT message_id FROM mentions WHERE org = ? AND user = ? "
                    "AND channel = ? AND message_id = ?",
                    (org, user, channel, message_id),
                )
                if not mentions:
                    raise ValueError("Subscribe to the channel or acknowledge a received mention.")
            await connection.execute(
                "UPDATE mentions SET is_read = 1 WHERE org = ? AND user = ? "
                "AND channel = ? AND is_read = 0 AND message_id <= ?",
                (org, user, channel, message_id),
            )
            if not subscriptions:
                rows = await self.rows(
                    connection,
                    "SELECT MAX(message_id) AS last_read_id FROM mentions "
                    "WHERE org = ? AND user = ? AND channel = ? AND is_read = 1",
                    (org, user, channel),
                )
                return {
                    "org": org,
                    "user": user,
                    "channel": channel,
                    "last_read_id": rows[0]["last_read_id"],
                    "subscribed": False,
                }
            await connection.execute(
                "UPDATE subscriptions SET last_read_id = MAX(last_read_id, ?) "
                "WHERE org = ? AND user = ? AND channel = ?",
                (message_id, org, user, channel),
            )
            return await self.require_subscription(connection, org, user, channel)

    async def search(
        self,
        org: str,
        query: str,
        channel: str | None = None,
        after: int = 0,
        limit: int = 50,
        *,
        user: str | None = None,
    ) -> list[dict[str, Any]]:
        self.check_page(after, limit)
        if not query.strip():
            raise ValueError("Search text must not be empty.")
        async with self.transaction() as connection:
            if channel is not None:
                await self.require_channel(connection, org, channel, user)
            try:
                # Evaluate MATCH first so invalid queries also fail in empty organizations.
                return await self.rows(
                    connection,
                    "SELECT m.* FROM messages_fts CROSS JOIN messages m "
                    "ON m.id = messages_fts.rowid "
                    "LEFT JOIN direct_conversations d ON d.org = m.org AND d.channel = m.channel "
                    "WHERE messages_fts MATCH ? AND m.org = ? "
                    "AND (d.channel IS NULL OR ? IN (d.user_a, d.user_b)) "
                    "AND (? IS NULL OR m.channel = ?) AND messages_fts.rowid > ? "
                    "ORDER BY messages_fts.rowid LIMIT ?",
                    (query, org, user, channel, channel, after, limit),
                )
            except aiosqlite.OperationalError as error:
                if str(error).startswith(
                    (
                        "fts5: syntax error",
                        "unterminated string",
                        "no such column:",
                        "unknown special query:",
                        "expected integer, got",
                    )
                ):
                    raise ValueError(
                        "Invalid full-text search query. Use words, quoted phrases, "
                        "AND/OR/NOT, or a prefix such as rele*."
                    ) from None
                raise
