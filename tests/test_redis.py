"""The bundled Redis backends, against the contracts in docs/deployment/stores.md.

Skipped where no Redis answers. Two `RedisStorage` objects on one server stand
in for two workers on two machines: separate connections, one database.

    docker run -d -p 6379:6379 redis:7-alpine

`REDIS_URL` names another server.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator

import pytest
from pydantic import BaseModel

from aiohttp_tiny_mcp import Client, Registry
from aiohttp_tiny_mcp.protocol.selection import AdapterSet
from aiohttp_tiny_mcp.storage.hub import START, Hub
from aiohttp_tiny_mcp.storage.redis import RedisHub, RedisSessionStore, RedisStorage
from aiohttp_tiny_mcp.storage.sessions import SessionStore
from aiohttp_tiny_mcp.testing import serving

pytestmark = pytest.mark.asyncio


class Add(BaseModel):
    a: int
    b: int


@pytest.fixture
def prefix() -> str:
    """Keys of one test, so a shared server does not mix two runs together."""
    return f"mcptest:{uuid.uuid4().hex[:12]}"


@pytest.fixture
async def storage(redis_url) -> AsyncIterator[RedisStorage]:
    held = RedisStorage.from_url(redis_url)
    try:
        yield held
    finally:
        await held.close()


@pytest.fixture
async def elsewhere(redis_url) -> AsyncIterator[RedisStorage]:
    """A second connection to the same server, standing in for another worker."""
    held = RedisStorage.from_url(redis_url)
    try:
        yield held
    finally:
        await held.close()


@pytest.fixture
def store(storage, prefix) -> RedisSessionStore:
    return RedisSessionStore(storage, prefix=prefix)


@pytest.fixture
def hub(storage, prefix) -> RedisHub:
    return RedisHub(storage, prefix=prefix)


async def test_the_backends_are_the_types_the_contracts_name():
    assert issubclass(RedisHub, Hub)
    assert issubclass(RedisSessionStore, SessionStore)


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


async def test_concurrent_saves_of_one_version_have_one_winner(store):
    """The check and the write are one script, so this cannot be a read
    followed by an update."""
    await store.create("s1", {"n": 0}, ttl_seconds=60)
    written = await asyncio.gather(
        *(store.save("s1", {"n": i}, expected_version=1, ttl_seconds=60) for i in range(20))
    )
    assert written.count(True) == 1


async def test_a_session_created_here_is_read_there(storage, elsewhere, prefix):
    await RedisSessionStore(storage, prefix=prefix).create("s1", {"n": 1}, ttl_seconds=60)
    record = await RedisSessionStore(elsewhere, prefix=prefix).get("s1")
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


async def test_a_waiting_reader_is_woken_rather_than_polled(hub):
    """`XREAD BLOCK` returns when the event lands, so this finishes in far
    less than the deadline it was given."""
    cursor = await hub.position("demo")

    async def publish_soon() -> None:
        await hub.publish("demo", {"n": 1})

    publishing = asyncio.ensure_future(publish_soon())
    events = await hub.poll("demo", cursor, timeout=30)
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


async def test_an_event_published_here_is_read_there(storage, elsewhere, prefix):
    reader = RedisHub(elsewhere, prefix=prefix)
    cursor = await reader.position("demo")
    await RedisHub(storage, prefix=prefix).publish("demo", {"n": 1})
    events = await reader.poll("demo", cursor, timeout=5)
    assert [event.message["n"] for event in events] == [1]


async def test_a_topic_is_trimmed_to_what_it_keeps(storage, prefix):
    """A hub is a log, and a log that nothing trims fills the server."""
    hub = RedisHub(storage, prefix=prefix, keep=10)
    for n in range(200):
        await hub.publish("demo", {"n": n})
    client = await storage.open()
    assert await client.xlen(hub.key("demo")) < 200


async def test_a_registry_runs_on_them(storage, elsewhere, prefix):
    """The contracts are what the server uses, so check them through it."""
    registry = Registry(
        "redis",
        "1.0",
        hub=RedisHub(storage, prefix=prefix),
        session_store=RedisSessionStore(storage, prefix=prefix),
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
    record = await RedisSessionStore(elsewhere, prefix=prefix).get(held)
    assert record is not None
    assert record.data["protocolVersion"] == "2025-11-25"


async def test_a_prefix_opens_the_keys(storage, prefix):
    """One Redis can hold two deployments, or this package beside another."""
    await RedisSessionStore(storage, prefix=prefix).create("s1", {"n": 1}, ttl_seconds=60)
    client = await storage.open()
    assert await client.exists(f"{prefix}:session:s1")
    assert not await client.exists("mcp:session:s1")


async def test_the_store_and_the_hub_take_the_prefix_from_the_storage(redis_url, prefix):
    """Set once where the connection is set, rather than on each of them."""
    storage = RedisStorage.from_url(redis_url, prefix=prefix)
    try:
        assert RedisSessionStore(storage).prefix == prefix
        assert RedisHub(storage).prefix == prefix
        assert RedisHub(storage, prefix="elsewhere").prefix == "elsewhere"
    finally:
        await storage.close()
