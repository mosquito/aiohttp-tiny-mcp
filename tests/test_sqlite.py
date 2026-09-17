"""The bundled SQLite backends, against the contracts in docs/deployment/stores.md.

Two `SqliteStorage` objects on one file stand in for two workers: separate
connections, one database. What they agree on here is what workers agree on.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest
from pydantic import BaseModel

from aiohttp_tiny_mcp import Client, Registry
from aiohttp_tiny_mcp.hub import START, Hub
from aiohttp_tiny_mcp.protocol.selection import AdapterSet
from aiohttp_tiny_mcp.sessions import SessionStore
from aiohttp_tiny_mcp.sqlite import NOW, SqliteHub, SqliteSessionStore, SqliteStorage
from aiohttp_tiny_mcp.testing import serving

pytestmark = pytest.mark.asyncio


class Add(BaseModel):
    a: int
    b: int


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


async def test_the_backends_are_the_types_the_contracts_name():
    """Subclassing is what has the methods checked and the missing ones refused."""
    assert issubclass(SqliteHub, Hub)
    assert issubclass(SqliteSessionStore, SessionStore)


async def test_the_timestamp_comes_from_the_database(storage):
    """Expiry is decided by one clock, so the time is the database's own.

    A NULL here would be silent: every comparison against it is false, so
    sessions would simply stop being found. `unixepoch('subsec')` reads NULL
    before SQLite 3.42, which is the kind of thing this catches.
    """
    import time

    connection = await storage.open()
    async with connection.execute(f"SELECT {NOW}, {NOW} = {NOW}") as cursor:
        row = await cursor.fetchone()
    assert row is not None
    now, agree = row
    assert now is not None, "the expression read NULL on this SQLite"
    assert agree == 1, "two readings in one statement must be the same instant"
    assert abs(now - time.time()) < 5
    assert isinstance(now, int), "whole seconds, not a float to be rounded later"


async def test_create_then_get_round_trips(store):
    assert await store.create("s1", {"n": 1}, ttl_seconds=60) is True
    record = await store.get("s1")
    assert record is not None
    assert record.data == {"n": 1}
    assert record.version == 1


async def test_get_unknown_session_is_none(store):
    assert await store.get("nope") is None


async def test_create_refuses_to_overwrite_a_live_record(store):
    assert await store.create("s1", {"n": 1}, ttl_seconds=60) is True
    assert await store.create("s1", {"n": 2}, ttl_seconds=60) is False
    record = await store.get("s1")
    assert record is not None and record.data == {"n": 1}


async def test_save_advances_the_version(store):
    await store.create("s1", {"n": 1}, ttl_seconds=60)
    assert await store.save("s1", {"n": 2}, expected_version=1, ttl_seconds=60) is True
    record = await store.get("s1")
    assert record is not None
    assert record.data == {"n": 2}
    assert record.version == 2


async def test_save_rejects_a_lost_update(store):
    await store.create("s1", {"n": 1}, ttl_seconds=60)
    await store.save("s1", {"n": 2}, expected_version=1, ttl_seconds=60)
    assert await store.save("s1", {"n": 3}, expected_version=1, ttl_seconds=60) is False


async def test_save_on_an_unknown_session_fails(store):
    assert await store.save("nope", {}, expected_version=1, ttl_seconds=60) is False


async def test_delete_is_idempotent(store):
    await store.create("s1", {}, ttl_seconds=60)
    await store.delete("s1")
    await store.delete("s1")
    assert await store.get("s1") is None


async def test_an_expired_record_reads_as_absent(store):
    await store.create("s1", {"n": 1}, ttl_seconds=0)
    assert await store.get("s1") is None


async def test_create_may_reuse_an_expired_key(store):
    await store.create("s1", {"n": 1}, ttl_seconds=0)
    assert await store.create("s1", {"n": 2}, ttl_seconds=60) is True
    record = await store.get("s1")
    assert record is not None
    assert record.data == {"n": 2}
    assert record.version == 1, "a reused key starts again"


async def test_save_does_not_resurrect_an_expired_record(store):
    await store.create("s1", {"n": 1}, ttl_seconds=0)
    assert await store.save("s1", {"n": 2}, expected_version=1, ttl_seconds=60) is False


async def test_concurrent_saves_of_one_version_have_one_winner(store):
    """The version is checked by the statement that writes, so this cannot be
    a read followed by an update."""
    await store.create("s1", {"n": 0}, ttl_seconds=60)
    written = await asyncio.gather(
        *(store.save("s1", {"n": i}, expected_version=1, ttl_seconds=60) for i in range(20))
    )
    assert written.count(True) == 1


async def test_a_session_created_here_is_read_there(storage, elsewhere):
    await SqliteSessionStore(storage).create("s1", {"n": 1}, ttl_seconds=60)
    record = await SqliteSessionStore(elsewhere).get("s1")
    assert record is not None and record.data == {"n": 1}


async def test_an_empty_topic_starts_at_the_beginning(hub):
    assert await hub.position("demo") == START


async def test_published_messages_are_read_in_order(hub):
    cursor = await hub.position("demo")
    for n in range(3):
        await hub.publish("demo", {"n": n})
    events = await hub.poll("demo", cursor, timeout=1)
    assert [event.message["n"] for event in events] == [0, 1, 2]

    assert await hub.poll("demo", events[-1].id, timeout=0.05) == []


async def test_an_empty_poll_keeps_the_readers_place(hub):
    cursor = await hub.position("demo")
    assert await hub.poll("demo", cursor, timeout=0.05) == []

    await hub.publish("demo", {"n": 1})
    events = await hub.poll("demo", cursor, timeout=1)
    assert [event.message["n"] for event in events] == [1]


async def test_a_message_published_before_the_first_poll_is_not_missed(hub):
    """The position is captured before the event, which is what makes the
    question-and-answer round trip free of a lost wake-up."""
    cursor = await hub.position("demo")
    await hub.publish("demo", {"n": 1})
    events = await hub.poll("demo", cursor, timeout=1)
    assert [event.message["n"] for event in events] == [1]


async def test_a_reader_waits_for_a_message_that_arrives_late(hub):
    cursor = await hub.position("demo")

    async def publish_soon() -> None:
        await hub.publish("demo", {"n": 1})

    publishing = asyncio.ensure_future(publish_soon())
    events = await hub.poll("demo", cursor, timeout=5)
    await publishing
    assert [event.message["n"] for event in events] == [1]


async def test_reading_does_not_consume(hub):
    """Several workers read one change, so a poll is not a queue."""
    first = await hub.position("demo")
    second = await hub.position("demo")
    await hub.publish("demo", {"n": 1})
    assert [event.message for event in await hub.poll("demo", first, timeout=1)] == [{"n": 1}]
    assert [event.message for event in await hub.poll("demo", second, timeout=1)] == [{"n": 1}]


async def test_topics_do_not_leak_into_each_other(hub):
    cursor = await hub.position("one")
    await hub.publish("two", {"n": 1})
    assert await hub.poll("one", cursor, timeout=0.05) == []


async def test_a_deleted_topic_is_forgotten(hub):
    await hub.publish("demo", {"n": 1})
    await hub.delete("demo")
    assert await hub.position("demo") == START


async def test_an_event_published_here_is_read_there(storage, elsewhere):
    reader = SqliteHub(elsewhere, look_again=0.01)
    cursor = await reader.position("demo")
    await SqliteHub(storage).publish("demo", {"n": 1})
    events = await reader.poll("demo", cursor, timeout=5)
    assert [event.message["n"] for event in events] == [1]


async def test_sweeping_removes_what_has_expired(file):
    storage = SqliteStorage(file, event_ttl_seconds=0)
    try:
        store = SqliteSessionStore(storage)
        hub = SqliteHub(storage)
        await store.create("live", {}, ttl_seconds=60)
        await store.create("dead", {}, ttl_seconds=0)
        await hub.publish("demo", {"n": 1})

        await storage.sweep()

        assert await store.get("live") is not None
        assert await store.get("dead") is None
        assert await hub.position("demo") == START
    finally:
        await storage.close()


async def test_cleanup_context_joins_the_sweeper(file):
    """The app must not close its connection while its sweep task still runs."""

    class ObservedStorage(SqliteStorage):
        def __init__(self, path: str) -> None:
            super().__init__(path)
            self.cancelled = asyncio.Event()
            self.release = asyncio.Event()
            self.stopped = asyncio.Event()

        async def sweeping(self, *, every: float = 60.0) -> None:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled.set()
                await self.release.wait()
                self.stopped.set()
                raise

    storage = ObservedStorage(file)
    context = storage.cleanup_ctx(None)
    await anext(context)
    closing = asyncio.create_task(anext(context))
    await asyncio.wait_for(storage.cancelled.wait(), 1)

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(asyncio.shield(closing), 0.01)

    storage.release.set()
    with pytest.raises(StopAsyncIteration):
        await closing
    assert storage.stopped.is_set()


async def test_a_registry_runs_on_them(storage, elsewhere):
    """The contracts are what the server uses, so check them through it."""
    registry = Registry(
        "sqlite",
        "1.0",
        hub=SqliteHub(storage, look_again=0.01),
        session_store=SqliteSessionStore(storage),
    )

    @registry.tool
    async def add(args: Add) -> int:
        """Add two integers."""
        return args.a + args.b

    async with serving(registry) as url:
        async with Client(url, AdapterSet.default().by_version["2025-11-25"]) as client:
            await client.initialize()
            held = client.session_id
            result = await client.call_tool("add", {"a": 2, "b": 3})

    assert result.content[0].text == "5"
    assert held is not None
    record = await SqliteSessionStore(elsewhere).get(held)
    assert record is not None
    assert record.data["protocolVersion"] == "2025-11-25"


async def test_the_package_does_not_need_aiosqlite_to_import():
    """The extra is optional, so nothing on the default path may import it."""
    import subprocess
    import sys

    asked = subprocess.run(
        [
            sys.executable,
            "-c",
            "import aiohttp_tiny_mcp, sys; print('aiosqlite' in sys.modules)",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert asked.stdout.strip() == "False"
