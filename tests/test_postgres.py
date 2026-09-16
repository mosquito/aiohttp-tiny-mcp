"""The bundled PostgreSQL backends, against the contracts in docs/deployment/stores.md.

Skipped where no PostgreSQL answers. Two `PostgresStorage` objects on one
server stand in for two workers on two machines: separate pools, one database.

    docker run -d -p 5432:5432 -e POSTGRES_USER=mcp -e POSTGRES_PASSWORD=mcp \
        -e POSTGRES_DB=mcp postgres:17-alpine

`POSTGRES_URL` names another server. The tables are shared, so each test names
its sessions and topics after itself.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator

import psycopg
import pytest
from pydantic import BaseModel

from aiohttp_tiny_mcp import Client, Registry
from aiohttp_tiny_mcp.hub import START, Hub
from aiohttp_tiny_mcp.postgres import PostgresHub, PostgresSessionStore, PostgresStorage
from aiohttp_tiny_mcp.protocol.selection import AdapterSet
from aiohttp_tiny_mcp.sessions import SessionStore
from aiohttp_tiny_mcp.testing import serving

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(15)]


class Add(BaseModel):
    a: int
    b: int


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


async def test_the_backends_are_the_types_the_contracts_name():
    assert issubclass(PostgresHub, Hub)
    assert issubclass(PostgresSessionStore, SessionStore)


async def test_create_then_get_round_trips(store, mine):
    assert await store.create(mine, {"n": 1}, ttl_seconds=60) is True
    record = await store.get(mine)
    assert record is not None
    assert record.data == {"n": 1}
    assert record.version == 1


async def test_get_unknown_session_is_none(store, mine):
    assert await store.get(mine) is None


async def test_create_refuses_to_overwrite_a_live_record(store, mine):
    assert await store.create(mine, {"n": 1}, ttl_seconds=60) is True
    assert await store.create(mine, {"n": 2}, ttl_seconds=60) is False
    record = await store.get(mine)
    assert record is not None and record.data == {"n": 1}


async def test_save_advances_the_version(store, mine):
    await store.create(mine, {"n": 1}, ttl_seconds=60)
    assert await store.save(mine, {"n": 2}, expected_version=1, ttl_seconds=60) is True
    record = await store.get(mine)
    assert record is not None
    assert record.data == {"n": 2}
    assert record.version == 2


async def test_save_rejects_a_lost_update(store, mine):
    await store.create(mine, {"n": 1}, ttl_seconds=60)
    await store.save(mine, {"n": 2}, expected_version=1, ttl_seconds=60)
    assert await store.save(mine, {"n": 3}, expected_version=1, ttl_seconds=60) is False


async def test_save_on_an_unknown_session_fails(store, mine):
    assert await store.save(mine, {}, expected_version=1, ttl_seconds=60) is False


async def test_delete_is_idempotent(store, mine):
    await store.create(mine, {}, ttl_seconds=60)
    await store.delete(mine)
    await store.delete(mine)
    assert await store.get(mine) is None


async def test_an_expired_record_reads_as_absent(store, mine):
    await store.create(mine, {"n": 1}, ttl_seconds=0)
    assert await store.get(mine) is None


async def test_create_may_reuse_an_expired_key(store, mine):
    await store.create(mine, {"n": 1}, ttl_seconds=0)
    assert await store.create(mine, {"n": 2}, ttl_seconds=60) is True
    record = await store.get(mine)
    assert record is not None
    assert record.data == {"n": 2}
    assert record.version == 1, "a reused key starts again"


async def test_save_does_not_resurrect_an_expired_record(store, mine):
    await store.create(mine, {"n": 1}, ttl_seconds=0)
    assert await store.save(mine, {"n": 2}, expected_version=1, ttl_seconds=60) is False


async def test_concurrent_saves_of_one_version_have_one_winner(store, mine):
    """The version is checked by the statement that writes, so this cannot be
    a read followed by an update."""
    await store.create(mine, {"n": 0}, ttl_seconds=60)
    written = await asyncio.gather(
        *(store.save(mine, {"n": i}, expected_version=1, ttl_seconds=60) for i in range(20))
    )
    assert written.count(True) == 1


async def test_a_session_created_here_is_read_there(storage, elsewhere, mine):
    await PostgresSessionStore(storage).create(mine, {"n": 1}, ttl_seconds=60)
    record = await PostgresSessionStore(elsewhere).get(mine)
    assert record is not None and record.data == {"n": 1}


async def test_an_empty_topic_starts_at_the_beginning(hub, mine):
    assert await hub.position(mine) == START


async def test_published_messages_are_read_in_order(hub, mine):
    cursor = await hub.position(mine)
    for n in range(3):
        await hub.publish(mine, {"n": n})
    messages, cursor = await hub.poll(mine, cursor, timeout=1)
    assert [message["n"] for message in messages] == [0, 1, 2]

    messages, cursor = await hub.poll(mine, cursor, timeout=0.05)
    assert messages == []


async def test_an_empty_poll_keeps_the_readers_place(hub, mine):
    cursor = await hub.position(mine)
    messages, after = await hub.poll(mine, cursor, timeout=0.05)
    assert messages == []
    assert after == cursor

    await hub.publish(mine, {"n": 1})
    messages, _ = await hub.poll(mine, after, timeout=1)
    assert [message["n"] for message in messages] == [1]


async def test_a_message_published_before_the_first_poll_is_not_missed(hub, mine):
    """The position is captured before the event, which is what makes the
    question-and-answer round trip free of a lost wake-up."""
    cursor = await hub.position(mine)
    await hub.publish(mine, {"n": 1})
    messages, _ = await hub.poll(mine, cursor, timeout=1)
    assert [message["n"] for message in messages] == [1]


async def test_a_waiting_reader_gets_what_arrives_late(hub, mine):
    """The reader asks again on a timer, so this finishes about one interval
    after the event lands rather than at the deadline."""
    cursor = await hub.position(mine)

    async def publish_soon() -> None:
        await hub.publish(mine, {"n": 1})

    publishing = asyncio.ensure_future(publish_soon())
    messages, _ = await hub.poll(mine, cursor, timeout=30)
    await publishing
    assert [message["n"] for message in messages] == [1]


async def test_reading_does_not_consume(hub, mine):
    """Several workers read one change, so a poll is not a queue."""
    first = await hub.position(mine)
    second = await hub.position(mine)
    await hub.publish(mine, {"n": 1})
    assert (await hub.poll(mine, first, timeout=1))[0] == [{"n": 1}]
    assert (await hub.poll(mine, second, timeout=1))[0] == [{"n": 1}]


async def test_topics_do_not_leak_into_each_other(hub, mine):
    cursor = await hub.position(f"{mine}-one")
    await hub.publish(f"{mine}-two", {"n": 1})
    assert (await hub.poll(f"{mine}-one", cursor, timeout=0.05))[0] == []


async def test_a_deleted_topic_is_forgotten(hub, mine):
    await hub.publish(mine, {"n": 1})
    await hub.delete(mine)
    assert await hub.position(mine) == START


async def test_an_event_published_here_is_read_there(storage, elsewhere, mine):
    reader = PostgresHub(elsewhere)
    cursor = await reader.position(mine)
    await PostgresHub(storage).publish(mine, {"n": 1})
    messages, _ = await reader.poll(mine, cursor, timeout=5)
    assert [message["n"] for message in messages] == [1]


async def test_concurrent_publishers_lose_nothing_to_the_cursor(storage, mine):
    """A sequence id alone would not order a topic: two transactions can take
    ids in one order and commit in the other, and a reader that moved past the
    higher id would never see the lower one. Publishing takes an advisory lock
    on the topic, so this cannot happen."""
    hub = PostgresHub(storage)
    cursor = await hub.position(mine)
    await asyncio.gather(*(hub.publish(mine, {"n": n}) for n in range(40)))

    seen: list[int] = []
    while len(seen) < 40:
        messages, cursor = await hub.poll(mine, cursor, timeout=5)
        if not messages:
            break
        seen += [message["n"] for message in messages]
    assert sorted(seen) == list(range(40))


async def test_sweeping_removes_what_has_expired(postgres_url, mine):
    storage = PostgresStorage.from_url(postgres_url, event_ttl_seconds=0)
    try:
        store = PostgresSessionStore(storage)
        hub = PostgresHub(storage)
        await store.create(f"{mine}-live", {}, ttl_seconds=60)
        await store.create(f"{mine}-dead", {}, ttl_seconds=0)
        await hub.publish(mine, {"n": 1})

        await storage.sweep()

        assert await store.get(f"{mine}-live") is not None
        assert await store.get(f"{mine}-dead") is None
        assert await hub.position(mine) == START
    finally:
        await storage.close()


async def test_a_registry_runs_on_them(storage, elsewhere):
    """The contracts are what the server uses, so check them through it."""
    registry = Registry(
        "postgres",
        "1.0",
        hub=PostgresHub(storage),
        session_store=PostgresSessionStore(storage),
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
    record = await PostgresSessionStore(elsewhere).get(held)
    assert record is not None
    assert record.data["protocolVersion"] == "2025-11-25"


async def test_a_prefix_names_the_tables(postgres_url, mine):
    """One database can hold two deployments, or this package beside another."""
    storage = PostgresStorage.from_url(postgres_url, prefix=f"other_{mine}")
    try:
        assert await PostgresSessionStore(storage).create(mine, {"n": 1}, ttl_seconds=60) is True
        pool = await storage.open()
        async with pool.connection() as connection:
            found = await connection.execute(
                "SELECT count(*) FROM information_schema.tables WHERE table_name = %s",
                (f"other_{mine}_sessions",),
            )
            assert (await found.fetchone())[0] == 1
            found = await connection.execute(
                "SELECT count(*) FROM mcp_sessions WHERE id = %s", (mine,)
            )
            assert (await found.fetchone())[0] == 0
        async with pool.connection() as connection:
            await connection.execute(f'DROP TABLE "other_{mine}_sessions", "other_{mine}_events"')
    finally:
        await storage.close()


async def test_the_tables_can_be_left_to_migrations(postgres_url, mine):
    """A deployment that owns its schema does not want them made behind it."""
    storage = PostgresStorage.from_url(postgres_url, prefix=f"absent_{mine}", create_tables=False)
    try:
        with pytest.raises(psycopg.errors.UndefinedTable):
            await PostgresSessionStore(storage).create(mine, {}, ttl_seconds=60)
    finally:
        await storage.close()


async def test_only_one_worker_sweeps_an_interval(postgres_url, mine):
    """Every worker may run this, and the work happens once. The lock settles
    who does it; the recorded time keeps the others from repeating it."""
    prefix = f"sweep{uuid.uuid4().hex[:8]}"
    workers = [
        PostgresStorage.from_url(postgres_url, prefix=prefix, event_ttl_seconds=0) for _ in range(5)
    ]
    try:
        store = PostgresSessionStore(workers[0])
        await store.create(f"{mine}-dead", {}, ttl_seconds=0)

        swept = await asyncio.gather(*(worker.sweep_if_due(every=60) for worker in workers))
        assert swept.count(True) == 1, swept
        assert await store.get(f"{mine}-dead") is None

        assert await workers[0].sweep_if_due(every=60) is False

        pool = await workers[0].open()
        async with pool.connection() as connection:
            await connection.execute(
                f'DROP TABLE "{prefix}_events", "{prefix}_sessions", "{prefix}_state"'
            )
    finally:
        for worker in workers:
            await worker.close()


async def test_a_sweep_that_has_never_run_is_due(postgres_url):
    storage = PostgresStorage.from_url(postgres_url, prefix=f"fresh{uuid.uuid4().hex[:8]}")
    try:
        await storage.open()
        assert await storage.due(3600) is True
        assert await storage.sweep_if_due(every=3600) is True
        assert await storage.due(3600) is False
        pool = await storage.open()
        async with pool.connection() as connection:
            await connection.execute(
                f'DROP TABLE "{storage.prefix}_events", "{storage.prefix}_sessions",'
                f' "{storage.prefix}_state"'
            )
    finally:
        await storage.close()
