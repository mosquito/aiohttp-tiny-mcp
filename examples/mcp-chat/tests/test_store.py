from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import aiosqlite
import pytest

from mcp_chat.store import SCHEMA, Store


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[Store]:
    storage = Store(tmp_path / "chat.sqlite3")
    await storage.open()
    try:
        yield storage
    finally:
        await storage.close()


async def test_concurrent_writes_keep_messages_and_unread_state(store: Store) -> None:
    await store.create_channel("example.com", "alice", "general")
    await store.subscribe("example.com", "bob", "general")
    messages = await asyncio.gather(
        *[
            store.post_message("example.com", "alice", "general", f"Message {number}")
            for number in range(20)
        ]
    )
    assert len({message["id"] for message in messages}) == 20
    assert len(await store.inbox("example.com", "bob")) == 20
    ids = sorted(message["id"] for message in messages)
    await asyncio.gather(
        store.mark_read("example.com", "bob", "general", ids[-1]),
        store.mark_read("example.com", "bob", "general", ids[0]),
    )
    assert await store.inbox("example.com", "bob") == []


async def test_invalid_references_do_not_change_channel_or_read_state(store: Store) -> None:
    await store.create_channel("example.com", "alice", "general")
    await store.create_channel("example.com", "alice", "other")
    await store.subscribe("example.com", "bob", "general")
    root = await store.post_message("example.com", "alice", "general", "Root")
    reply = await store.post_message("example.com", "alice", "general", "Reply", root["id"])
    foreign = await store.post_message("example.com", "alice", "other", "Different channel")
    with pytest.raises(ValueError):
        await store.mark_read("example.com", "bob", "general", foreign["id"])
    with pytest.raises(ValueError):
        await store.mark_read("example.com", "bob", "general", foreign["id"] + 1)
    with pytest.raises(ValueError):
        await store.post_message("example.com", "alice", "general", "Wrong parent", foreign["id"])
    with pytest.raises(ValueError):
        await store.post_message("example.com", "alice", "general", "Nested reply", reply["id"])
    assert len(await store.inbox("example.com", "bob")) == 2
    assert len(await store.history("example.com", "general")) == 2
    assert await store.post_message("example.com", "alice", "general", "Still usable")


async def test_duplicate_create_does_not_subscribe_another_user(store: Store) -> None:
    await store.create_channel("example.com", "alice", "general")
    with pytest.raises(ValueError):
        await store.create_channel("example.com", "bob", "general")
    assert await store.list_subscriptions("example.com", "bob") == []
    await store.subscribe("example.com", "bob", "general")
    assert len(await store.list_subscriptions("example.com", "bob")) == 1


async def test_fts_search_words_phrases_prefixes_and_unicode(store: Store) -> None:
    await store.create_channel("example.com", "alice", "general")
    release = await store.post_message("example.com", "alice", "general", "Release plan for café")
    discussion = await store.post_message(
        "example.com", "alice", "general", "Plan the next release"
    )
    cyrillic = await store.post_message(
        "example.com", "alice", "general", "Проверка индекса поиска"
    )
    punctuation = await store.post_message("example.com", "alice", "general", "snake_case at 100%")
    assert [row["id"] for row in await store.search("example.com", "release plan")] == [
        release["id"],
        discussion["id"],
    ]
    assert [row["id"] for row in await store.search("example.com", '"release plan"')] == [
        release["id"]
    ]
    assert [row["id"] for row in await store.search("example.com", "cafe")] == [release["id"]]
    assert [row["id"] for row in await store.search("example.com", "ПРОВЕРКА")] == [cyrillic["id"]]
    assert [row["id"] for row in await store.search("example.com", "rele* NOT next")] == [
        release["id"]
    ]
    assert [row["id"] for row in await store.search("example.com", '"snake_case" OR "100%"')] == [
        punctuation["id"]
    ]
    assert await store.search("example.com", "lease") == []


@pytest.mark.parametrize(
    "query",
    ['"unclosed', "release AND", "org:example", "%", "release-plan", "NEAR(release plan, nope)"],
)
async def test_fts_invalid_query_is_clear_and_keeps_store_usable(store: Store, query: str) -> None:
    await store.create_channel("example.com", "alice", "general")
    message = await store.post_message("example.com", "alice", "general", "Release plan")
    with pytest.raises(ValueError, match="Invalid full-text search query"):
        await store.search("example.com", query)
    assert [row["id"] for row in await store.search("example.com", "release")] == [message["id"]]


async def test_resubscribe_skips_messages_sent_while_unsubscribed(store: Store) -> None:
    await store.create_channel("example.com", "alice", "general")
    await store.subscribe("example.com", "bob", "general")
    await store.post_message("example.com", "alice", "general", "Before leaving")
    await store.unsubscribe("example.com", "bob", "general")
    await store.post_message("example.com", "alice", "general", "While away")
    await store.subscribe("example.com", "bob", "general")
    assert await store.inbox("example.com", "bob") == []
    message = await store.post_message("example.com", "alice", "general", "After joining")
    assert [row["id"] for row in await store.inbox("example.com", "bob")] == [message["id"]]


async def test_thread_and_search_support_cursor_pages(store: Store) -> None:
    await store.create_channel("example.com", "alice", "general")
    root = await store.post_message("example.com", "alice", "general", "Match root")
    first = await store.post_message("example.com", "alice", "general", "Match first", root["id"])
    await store.post_message("example.com", "alice", "general", "Unrelated message")
    last = await store.post_message("example.com", "alice", "general", "Match last", root["id"])
    thread = await store.thread("example.com", root["id"], after=first["id"], limit=1)
    assert [row["id"] for row in thread] == [last["id"]]
    matches = await store.search("example.com", "Match", after=root["id"], limit=1)
    assert [row["id"] for row in matches] == [first["id"]]


async def test_default_channels_preserve_existing_data_and_memberships(store: Store) -> None:
    await store.create_channel("example.com", "alice", "general", "Existing channel")
    message = await store.post_message("example.com", "alice", "general", "Existing message")
    await store.ensure_organization("example.com")
    await store.unsubscribe("example.com", "alice", "general")
    await store.ensure_organization("example.com")
    channels = {row["name"]: row for row in await store.list_channels("example.com", "alice")}
    assert set(channels) == {"general", "random", "tasks"}
    assert channels["general"]["description"] == "Existing channel"
    assert channels["general"]["created_by"] == "alice"
    assert not any(row["subscribed"] for row in channels.values())
    assert await store.history("example.com", "general") == [message]
    await store.ensure_organization("other.com")
    assert await store.history("other.com", "general") == []
    assert await store.list_subscriptions("other.com", "alice") == []


async def test_concurrent_default_creation_across_connections(tmp_path: Path) -> None:
    first = Store(tmp_path / "shared.sqlite3")
    second = Store(first.path)
    await first.open()
    await second.open()
    try:
        await asyncio.gather(
            *[storage.ensure_organization("example.com") for storage in [first, second] * 5]
        )
        channels = await second.list_channels("example.com", "alice")
        assert [row["name"] for row in channels] == ["general", "random", "tasks"]
        assert not any(row["subscribed"] for row in channels)
    finally:
        await first.close()
        await second.close()


async def test_fts_filters_org_channel_and_pages_thread_replies(store: Store) -> None:
    for org in ["example.com", "other.com"]:
        for channel in ["general", "tasks"]:
            await store.create_channel(org, "alice", channel)
    root = await store.post_message("example.com", "alice", "general", "Release root")
    await store.post_message("other.com", "alice", "general", "Release foreign")
    reply = await store.post_message("example.com", "alice", "general", "Release reply", root["id"])
    task = await store.post_message("example.com", "alice", "tasks", "Release tasks")
    assert [row["id"] for row in await store.search("example.com", "release")] == [
        root["id"],
        reply["id"],
        task["id"],
    ]
    assert [row["id"] for row in await store.search("example.com", "release", "tasks")] == [
        task["id"]
    ]
    page = await store.search("example.com", "release", "general", after=root["id"], limit=1)
    assert [row["id"] for row in page] == [reply["id"]]
    assert len(await store.search("other.com", "release")) == 1


async def test_fts_backfills_existing_database_and_survives_reopen(tmp_path: Path) -> None:
    path = tmp_path / "old-chat.sqlite3"
    async with aiosqlite.connect(path, isolation_level=None) as connection:
        await connection.executescript(SCHEMA)
        await connection.execute(
            "INSERT INTO channels (org,name,description,created_by) "
            "VALUES ('example.com','general','','alice')"
        )
        await connection.execute(
            "INSERT INTO messages (org,channel,user,text) "
            "VALUES ('example.com','general','alice','Older release')"
        )
    first = Store(path)
    second = Store(path)
    try:
        await asyncio.gather(first.open(), second.open())
        matches = await first.search("example.com", "older")
        assert len(matches) == 1
        assert matches[0]["text"] == "Older release"
        await first.subscribe("example.com", "alice", "general")
        added = await first.post_message("example.com", "alice", "general", "New release")
        assert len(await second.search("example.com", "release")) == 2
    finally:
        await first.close()
        await second.close()
    await first.open()
    try:
        assert [row["id"] for row in await first.search("example.com", "new")] == [added["id"]]
        assert len(await first.search("example.com", "release")) == 2
    finally:
        await first.close()


async def test_concurrent_open_handles_wal_transition(tmp_path: Path) -> None:
    for number in range(25):
        path = tmp_path / f"startup-{number}.sqlite3"
        async with aiosqlite.connect(path) as connection:
            async with connection.executescript(SCHEMA):
                pass
        stores = [Store(path), Store(path)]
        try:
            await asyncio.gather(*(store.open() for store in stores))
            for store in stores:
                assert store.connection is not None
                async with store.connection.execute("PRAGMA journal_mode") as cursor:
                    row = await cursor.fetchone()
                    assert row is not None
                    assert row[0] == "wal"
        finally:
            await asyncio.gather(*(store.close() for store in stores))


async def test_fts_triggers_track_updates_and_deletes(store: Store) -> None:
    await store.create_channel("example.com", "alice", "general")
    message = await store.post_message("example.com", "alice", "general", "Old wording")
    async with store.transaction() as connection:
        await connection.execute(
            "UPDATE messages SET text = ? WHERE id = ?", ("New wording", message["id"])
        )
    assert await store.search("example.com", "old") == []
    assert [row["id"] for row in await store.search("example.com", "new")] == [message["id"]]
    async with store.transaction() as connection:
        await connection.execute("DELETE FROM messages WHERE id = ?", (message["id"],))
    assert await store.search("example.com", "new") == []
