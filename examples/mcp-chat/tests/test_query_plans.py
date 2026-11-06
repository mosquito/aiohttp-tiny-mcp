from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import aiosqlite
import pytest

from mcp_chat.store import Store


class QueryStore(Store):
    def __init__(self, path: Path) -> None:
        super().__init__(path)
        self.queries: list[tuple[str, tuple[Any, ...]]] = []

    async def rows(
        self, connection: aiosqlite.Connection, sql: str, parameters: tuple[Any, ...]
    ) -> list[dict[str, Any]]:
        self.queries.append((sql, parameters))
        return await Store.rows(connection, sql, parameters)

    async def plan(self, query: tuple[str, tuple[Any, ...]]) -> str:
        assert self.connection is not None
        sql, parameters = query
        rows = await Store.rows(self.connection, "EXPLAIN QUERY PLAN " + sql, parameters)
        return "\n".join(row["detail"] for row in rows)


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[QueryStore]:
    result = QueryStore(tmp_path / "plans.sqlite3")
    await result.open()
    await result.create_channel("example.com", "alice", "general")
    try:
        yield result
    finally:
        await result.close()


async def test_thread_seeks_root_and_replies_instead_of_organization(store: QueryStore) -> None:
    root = await store.post_message("example.com", "alice", "general", "root")
    reply = await store.post_message("example.com", "alice", "general", "reply", root["id"])
    assert [row["id"] for row in await store.thread("example.com", root["id"])] == [
        root["id"],
        reply["id"],
    ]
    plan = await store.plan(store.queries[-1])
    assert "messages_thread" in plan
    assert "messages_org_id" not in plan


async def test_fts_uses_ordered_cursor_without_sorting_all_matches(store: QueryStore) -> None:
    root = await store.post_message("example.com", "alice", "general", "common token")
    reply = await store.post_message("example.com", "alice", "general", "common token")
    result = await store.search("example.com", "common", after=root["id"])
    assert [row["id"] for row in result] == [reply["id"]]
    plan = await store.plan(store.queries[-1])
    assert "messages_fts VIRTUAL TABLE" in plan
    assert "TEMP B-TREE FOR ORDER BY" not in plan


async def test_observe_filters_use_user_and_tool_indexes(store: QueryStore) -> None:
    await store.record_event("example.com", "alice", "post_message", {}, {}, None)
    await store.record_event("example.com", "bob", "send_dm", {}, {}, None)
    await store.record_event("example.com", "alice", "inbox", {}, {}, None)
    assert await store.latest_event("example.com", "alice", tools=["post_message", "send_dm"]) == 1
    assert "audit_org_user_tool_id" in await store.plan(store.queries[-1])
    assert await store.latest_event("example.com", None, tools=["post_message", "send_dm"]) == 2
    assert "audit_org_tool_id" in await store.plan(store.queries[-1])
    result = await store.observe("example.com", "alice", before=3)
    assert [event["id"] for event in result["events"]] == [1]
    event_query = next(
        query for query in reversed(store.queries) if query[0].startswith("SELECT a.*")
    )
    plan = await store.plan(event_query)
    assert "audit_org_user_id" in plan
    assert "id<?" in plan


async def test_inbox_work_is_bounded_for_large_unread_backlog(store: QueryStore) -> None:
    await store.subscribe("example.com", "bob", "general")
    assert store.connection is not None
    async with store.transaction() as connection:
        await connection.executemany(
            "INSERT INTO messages(org,channel,user,text) VALUES (?,?,?,?)",
            [("example.com", "general", "alice", "unread") for number in range(20000)],
        )
    callbacks = 0

    def progress() -> int:
        nonlocal callbacks
        callbacks += 1
        return 0

    await store.connection.set_progress_handler(progress, 1000)
    try:
        page = await store.inbox("example.com", "bob", limit=10)
    finally:
        await store.connection.set_progress_handler(progress, 0)
    assert [message["id"] for message in page] == list(range(1, 11))
    assert callbacks < 20
    await store.mark_read("example.com", "bob", "general", 20000)
    last = await store.post_message("example.com", "alice", "general", "last unread")
    callbacks = 0
    await store.connection.set_progress_handler(progress, 1000)
    try:
        page = await store.inbox("example.com", "bob", limit=10)
    finally:
        await store.connection.set_progress_handler(progress, 0)
    assert [message["id"] for message in page] == [last["id"]]
    assert callbacks < 20


async def test_inbox_limits_apply_after_excluding_own_messages(store: QueryStore) -> None:
    await store.subscribe("example.com", "bob", "general")
    async with store.transaction() as connection:
        await connection.executemany(
            "INSERT INTO messages(org,channel,user,text) VALUES (?,?,?,?)",
            [("example.com", "general", "bob", "own") for number in range(500)],
        )
    first = await store.post_message("example.com", "alice", "general", "@bob first")
    second = await store.post_message("example.com", "alice", "general", "@bob second")
    assert [row["id"] for row in await store.inbox("example.com", "bob", limit=1)] == [first["id"]]
    assert [
        row["id"] for row in await store.inbox("example.com", "bob", after=first["id"], limit=1)
    ] == [second["id"]]
