"""`MemoryHub` across threads, each running an event loop of its own.

One process may run more than one event loop: a worker thread that calls
`asyncio.run`, a test harness beside an application, an embedded server. The
hub is process-local storage, so every one of those loops reaches the same
instance. These check that it serves them all.
"""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Any

import pytest

from aiohttp_tiny_mcp.storage.hub import MemoryHub

pytestmark = pytest.mark.timeout(30)


def in_a_loop_of_its_own(work: Any, into: dict[str, Any], key: str) -> threading.Thread:
    """Run `work()` under `asyncio.run` in a new thread, and keep its result."""

    def run() -> None:
        try:
            into[key] = asyncio.run(work())
        except BaseException as failure:  # noqa: BLE001 -- the failure is the result
            into[key] = failure

    return threading.Thread(target=run, daemon=True)


def test_a_second_loop_may_wait_on_the_same_hub():
    """A waiting `poll` must not tie the hub to the loop that waited first.

    An `asyncio.Condition` binds to that loop and raises for every other one,
    which reaches any deployment where two loops share a registry.
    """
    hub = MemoryHub()
    outcome: dict[str, Any] = {}

    async def waits() -> list[Any]:
        return list(await hub.poll("quiet", "", timeout=0.05))

    for key in ("first", "second"):
        thread = in_a_loop_of_its_own(waits, outcome, key)
        thread.start()
        thread.join(10)

    assert outcome == {"first": [], "second": []}, outcome


def test_a_publish_wakes_a_reader_on_another_loop():
    """The reader must return when the event lands, not when its wait expires."""
    hub = MemoryHub()
    outcome: dict[str, Any] = {}
    subscribed = threading.Event()

    async def reads() -> list[Any]:
        subscription = await hub.subscribe("t", wait=3.0)
        subscribed.set()
        return [event.message for event in await subscription.poll(timeout=3.0)]

    async def writes() -> str:
        assert subscribed.wait(10)
        await asyncio.sleep(0.05)
        return await hub.publish("t", {"n": 9})

    reader = in_a_loop_of_its_own(reads, outcome, "read")
    writer = in_a_loop_of_its_own(writes, outcome, "write")
    started = time.perf_counter()
    reader.start()
    writer.start()
    reader.join(10)
    writer.join(10)
    elapsed = time.perf_counter() - started

    assert outcome["read"] == [{"n": 9}], outcome
    assert outcome["write"] == "1"
    assert elapsed < 1.0, f"the reader waited out its timeout instead of waking: {elapsed:.2f}s"


def test_concurrent_publishes_keep_every_event_and_its_own_id():
    """Ids come from one counter, so every publisher must see the same one.

    Under free-threaded Python the threads run at once and an unguarded
    counter loses events. Under a GIL build this passes either way; it is
    here for the interpreter that does not have one.
    """
    hub = MemoryHub(keep=10_000)
    each, threads = 500, 4
    outcome: dict[str, Any] = {}

    async def publishes() -> int:
        for _ in range(each):
            await hub.publish("t", {})
        return each

    running = [in_a_loop_of_its_own(publishes, outcome, str(number)) for number in range(threads)]
    for thread in running:
        thread.start()
    for thread in running:
        thread.join(20)

    assert all(result == each for result in outcome.values()), outcome
    rows = hub.rows["t"]
    assert len(rows) == each * threads
    assert len({event.id for event in rows}) == each * threads
    assert hub.last_id == each * threads


def test_a_closed_loop_does_not_break_a_later_publish():
    """A reader whose loop has gone must not take the publisher down with it."""
    hub = MemoryHub()
    outcome: dict[str, Any] = {}

    async def waits() -> list[Any]:
        return list(await hub.poll("t", "", timeout=0.05))

    thread = in_a_loop_of_its_own(waits, outcome, "gone")
    thread.start()
    thread.join(10)

    closed = asyncio.new_event_loop()
    closed.close()
    hub.waiters.add((closed, asyncio.Event()))

    async def publishes() -> str:
        return await hub.publish("t", {"n": 1})

    published: dict[str, Any] = {}
    thread = in_a_loop_of_its_own(publishes, published, "id")
    thread.start()
    thread.join(10)

    assert published["id"] == "1", published
