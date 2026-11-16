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
    "send_to_channel INTEGER NOT NULL DEFAULT 0 CHECK (send_to_channel IN (0, 1)), "
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
    "CREATE TABLE IF NOT EXISTS chat_schema (version INTEGER PRIMARY KEY);"
    "CREATE TABLE IF NOT EXISTS deliveries ("
    "org TEXT NOT NULL, user TEXT NOT NULL, channel TEXT NOT NULL, message_id INTEGER NOT NULL, "
    "is_read INTEGER NOT NULL DEFAULT 0 CHECK (is_read IN (0, 1)), "
    "durable INTEGER NOT NULL DEFAULT 0 CHECK (durable IN (0, 1)), "
    "PRIMARY KEY (org, user, message_id), "
    "FOREIGN KEY (org, channel, message_id) REFERENCES messages (org, channel, id));"
    "CREATE INDEX IF NOT EXISTS deliveries_unread ON deliveries (org, user, is_read, message_id);"
    "CREATE INDEX IF NOT EXISTS deliveries_channel "
    "ON deliveries (org, user, channel, is_read, message_id);"
    "CREATE INDEX IF NOT EXISTS deliveries_message ON deliveries (org, channel, message_id, user);"
    "CREATE INDEX IF NOT EXISTS messages_authors ON messages (org, parent_id, user);"
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
                await self.upgrade_routing(connection)
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

    async def upgrade_routing(self, connection: aiosqlite.Connection) -> None:
        """Preserve existing messages and unread state when enabling explicit delivery."""
        columns = await self.rows(connection, "PRAGMA table_info(messages)", ())
        if not any(column["name"] == "send_to_channel" for column in columns):
            await connection.execute(
                "ALTER TABLE messages ADD COLUMN send_to_channel INTEGER NOT NULL DEFAULT 0 "
                "CHECK (send_to_channel IN (0, 1))"
            )
            await connection.execute(
                "UPDATE messages SET send_to_channel = 1 WHERE parent_id IS NOT NULL"
            )
        await connection.execute(
            "CREATE INDEX IF NOT EXISTS messages_feed ON messages (org, channel, id) "
            "WHERE parent_id IS NULL OR send_to_channel = 1"
        )
        if await self.rows(connection, "SELECT version FROM chat_schema WHERE version = 1", ()):
            return
        await connection.execute(
            "INSERT INTO deliveries (org, user, channel, message_id) "
            "SELECT m.org, s.user, m.channel, m.id FROM subscriptions s JOIN messages m "
            "ON m.org = s.org AND m.channel = s.channel AND m.id > s.last_read_id "
            "LEFT JOIN direct_conversations d ON d.org = m.org AND d.channel = m.channel "
            "WHERE m.user != s.user AND (d.channel IS NULL OR s.user IN (d.user_a, d.user_b))"
        )
        await connection.execute(
            "INSERT INTO deliveries (org, user, channel, message_id, is_read, durable) "
            "SELECT n.org, n.user, n.channel, n.message_id, n.is_read, 1 FROM mentions n "
            "JOIN messages m ON m.id = n.message_id AND m.org = n.org AND m.channel = n.channel "
            "WHERE m.user != n.user AND NOT EXISTS (SELECT 1 FROM direct_conversations d "
            "WHERE d.org = n.org AND d.channel = n.channel) "
            "ON CONFLICT (org, user, message_id) DO UPDATE SET "
            "durable = 1, is_read = min(deliveries.is_read, excluded.is_read)"
        )
        await connection.execute("INSERT INTO chat_schema (version) VALUES (1)")

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

    async def decorate_messages(
        self, connection: aiosqlite.Connection, messages: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        roots: dict[str, list[int]] = {}
        for message in messages:
            message["send_to_channel"] = bool(message["send_to_channel"])
            message["reply_count"] = 0
            if message["parent_id"] is None:
                roots.setdefault(message["org"], []).append(message["id"])
        counts: dict[int, int] = {}
        for org, ids in roots.items():
            placeholders = ",".join("?" for number in ids)
            rows = await self.rows(
                connection,
                "SELECT parent_id, count(*) AS replies FROM messages WHERE org = ? "
                f"AND parent_id IN ({placeholders}) GROUP BY parent_id",
                (org, *ids),
            )
            counts.update((row["parent_id"], row["replies"]) for row in rows)
        for message in messages:
            message["reply_count"] = counts.get(message["id"], 0)
        return messages

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
                "(SELECT count(*) FROM deliveries n WHERE n.org = c.org AND n.channel = c.name "
                "AND n.user = ? AND n.is_read = 0) AS unread "
                "FROM channels c LEFT JOIN subscriptions s "
                "ON s.org = c.org AND s.channel = c.name AND s.user = ? "
                "WHERE c.org = ? AND NOT EXISTS (SELECT 1 FROM direct_conversations d "
                "WHERE d.org = c.org AND d.channel = c.name) ORDER BY c.name",
                (user, user, org),
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
            await connection.execute(
                "DELETE FROM deliveries WHERE org = ? AND user = ? AND channel = ? AND durable = 0",
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
            "s.last_read_id, (SELECT count(*) FROM deliveries n WHERE n.org = c.org "
            "AND n.channel = c.name AND n.user = ? AND n.is_read = 0) AS unread "
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
        self,
        org: str,
        user: str,
        peer: str,
        text: str,
        parent_id: int | None = None,
        send_to_channel: bool = False,
    ) -> dict[str, Any]:
        if send_to_channel and parent_id is None:
            raise ValueError("send_to_channel requires a thread reply.")
        conversation = await self.open_dm(org, user, peer)
        return await self.post_message(
            org, user, conversation["name"], text, parent_id, send_to_channel
        )

    async def is_direct(self, org: str, user: str, channel: str) -> bool:
        async with self.transaction() as connection:
            found = await self.require_channel(connection, org, channel, user)
            return found["kind"] == "dm"

    async def notification_recipients(
        self, org: str, user: str, channel: str, message_id: int | None = None
    ) -> dict[str, Any]:
        """Return the stored delivery audience and author for one accessible message."""
        async with self.transaction() as connection:
            found = await self.require_channel(connection, org, channel, user)
            viewers = None
            if found["kind"] == "dm":
                participants = await self.rows(
                    connection,
                    "SELECT user_a, user_b FROM direct_conversations WHERE org = ? AND channel = ?",
                    (org, channel),
                )
                viewers = [participants[0]["user_a"], participants[0]["user_b"]]
            if message_id is None:
                rows = await self.rows(
                    connection,
                    "SELECT user FROM subscriptions WHERE org = ? AND channel = ? ORDER BY user",
                    (org, channel),
                )
                return {
                    "kind": found["kind"],
                    "users": [row["user"] for row in rows],
                    "viewers": viewers,
                }
            message = await self.require_message(connection, org, message_id, user)
            if message["channel"] != channel:
                raise ValueError("Message not found in this channel.")
            rows = await self.rows(
                connection,
                "SELECT user FROM deliveries WHERE org = ? AND channel = ? AND message_id = ?",
                (org, channel, message_id),
            )
            users = {row["user"] for row in rows} | {message["user"]}
            return {"kind": found["kind"], "users": sorted(users), "viewers": viewers}

    async def post_message(
        self,
        org: str,
        user: str,
        channel: str,
        text: str,
        parent_id: int | None = None,
        send_to_channel: bool = False,
    ) -> dict[str, Any]:
        if send_to_channel and parent_id is None:
            raise ValueError("send_to_channel requires a thread reply.")
        async with self.transaction() as connection:
            await self.require_subscription(connection, org, user, channel)
            durable = self.mentioned_users(text)
            if parent_id is not None:
                parent = await self.require_message(connection, org, parent_id, user)
                if parent["channel"] != channel or parent["parent_id"] is not None:
                    raise ValueError("Reply to a root message in this channel.")
                authors = await self.rows(
                    connection,
                    "SELECT DISTINCT user FROM messages WHERE org = ? AND parent_id = ?",
                    (org, parent_id),
                )
                durable.update(row["user"] for row in authors)
                durable.add(parent["user"])
            recipients = set(durable)
            if parent_id is None or send_to_channel:
                members = await self.rows(
                    connection,
                    "SELECT user FROM subscriptions WHERE org = ? AND channel = ?",
                    (org, channel),
                )
                recipients.update(row["user"] for row in members)
            found = await self.require_channel(connection, org, channel, user)
            if found["kind"] == "dm":
                participants = await self.rows(
                    connection,
                    "SELECT user_a, user_b FROM direct_conversations WHERE org = ? AND channel = ?",
                    (org, channel),
                )
                permitted = {participants[0]["user_a"], participants[0]["user_b"]}
                recipients.intersection_update(permitted)
            recipients.discard(user)
            async with connection.execute(
                "INSERT INTO messages (org, user, channel, text, parent_id, send_to_channel) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (org, user, channel, text, parent_id, send_to_channel),
            ) as cursor:
                message_id = cursor.lastrowid
            assert message_id is not None
            await connection.executemany(
                "INSERT INTO deliveries (org, user, channel, message_id, durable) "
                "VALUES (?, ?, ?, ?, ?)",
                [
                    (org, recipient, channel, message_id, recipient in durable)
                    for recipient in sorted(recipients)
                ],
            )
            message = await self.require_message(connection, org, message_id, user)
            return (await self.decorate_messages(connection, [message]))[0]

    async def history(
        self, org: str, channel: str, after: int = 0, limit: int = 50, *, user: str | None = None
    ) -> list[dict[str, Any]]:
        self.check_page(after, limit)
        async with self.transaction() as connection:
            await self.require_channel(connection, org, channel, user)
            messages = await self.rows(
                connection,
                "SELECT * FROM messages WHERE org = ? AND channel = ? AND id > ? "
                "AND (parent_id IS NULL OR send_to_channel = 1) ORDER BY id LIMIT ?",
                (org, channel, after, limit),
            )
            return await self.decorate_messages(connection, messages)

    async def thread(
        self, org: str, message_id: int, after: int = 0, limit: int = 50, *, user: str | None = None
    ) -> list[dict[str, Any]]:
        self.check_page(after, limit)
        async with self.transaction() as connection:
            message = await self.require_message(connection, org, message_id, user)
            if message["parent_id"] is not None:
                raise ValueError("Use the root message ID to read a thread.")
            messages = await self.rows(
                connection,
                "SELECT * FROM messages WHERE org = ? AND id = ? AND id > ? "
                "UNION ALL SELECT * FROM messages WHERE org = ? AND parent_id = ? "
                "AND id > ? ORDER BY id LIMIT ?",
                (org, message_id, after, org, message_id, after, limit),
            )
            return await self.decorate_messages(connection, messages)

    async def inbox(
        self, org: str, user: str, after: int = 0, limit: int = 50
    ) -> list[dict[str, Any]]:
        self.check_page(after, limit)
        async with self.transaction() as connection:
            messages = await self.rows(
                connection,
                "SELECT m.* FROM deliveries n CROSS JOIN messages m ON m.id = n.message_id "
                "LEFT JOIN direct_conversations d ON d.org = m.org AND d.channel = m.channel "
                "WHERE n.org = ? AND n.user = ? AND n.is_read = 0 AND n.message_id > ? "
                "AND m.org = n.org AND m.channel = n.channel AND m.user != ? "
                "AND (d.channel IS NULL OR ? IN (d.user_a, d.user_b)) "
                "ORDER BY n.message_id LIMIT ?",
                (org, user, after, user, user, limit),
            )
            return await self.decorate_messages(connection, messages)

    async def mark_read(
        self, org: str, user: str, channel: str, message_id: int | list[int]
    ) -> dict[str, Any]:
        ids = message_id if isinstance(message_id, list) else [message_id]
        if (
            not ids
            or len(ids) > 100
            or any(type(number) is not int or number < 1 for number in ids)
        ):
            raise ValueError("Use 1–100 positive message IDs.")
        ids = list(dict.fromkeys(ids))
        async with self.transaction() as connection:
            await self.require_channel(connection, org, channel, user)
            placeholders = ",".join("?" for number in ids)
            found = await self.rows(
                connection,
                f"SELECT id FROM messages WHERE org = ? AND channel = ? AND id IN ({placeholders})",
                (org, channel, *ids),
            )
            if len(found) != len(ids):
                raise ValueError("Message not found in this channel.")
            await connection.execute(
                "UPDATE deliveries SET is_read = 1 "
                "WHERE org = ? AND user = ? AND channel = ? "
                f"AND message_id IN ({placeholders})",
                (org, user, channel, *ids),
            )
            await connection.execute(
                "UPDATE subscriptions SET last_read_id = MAX(last_read_id, ?) "
                "WHERE org = ? AND user = ? AND channel = ?",
                (max(ids), org, user, channel),
            )
            subscriptions = await self.rows(
                connection,
                "SELECT last_read_id FROM subscriptions WHERE org = ? AND user = ? AND channel = ?",
                (org, user, channel),
            )
            rows = await self.rows(
                connection,
                "SELECT MAX(message_id) AS last_read_id FROM deliveries "
                "WHERE org = ? AND user = ? AND channel = ? AND is_read = 1",
                (org, user, channel),
            )
            return {
                "org": org,
                "user": user,
                "channel": channel,
                "message_ids": ids,
                "last_read_id": max(
                    rows[0]["last_read_id"] or 0,
                    subscriptions[0]["last_read_id"] if subscriptions else 0,
                ),
                "subscribed": bool(subscriptions),
            }

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
                messages = await self.rows(
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
                return await self.decorate_messages(connection, messages)
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
