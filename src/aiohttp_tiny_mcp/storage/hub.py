"""Cursor-based event storage shared by reply and notification streams.

Applications supply the backend; MemoryHub serves one process. Shared storage lets one node
publish replies that another is awaiting.
"""

from __future__ import annotations

import asyncio
import threading
from abc import abstractmethod
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from .namespaces import scoped

Cursor = str

START: Cursor = ""

NOTIFICATIONS = "notifications"

ASK = "ask"

MEMORY_KEEP = 1000


@dataclass(frozen=True, slots=True)
class Event:
    """One published message and its id. The id is the cursor after it."""

    id: Cursor
    message: Mapping[str, Any]


class Subscription:
    """A reader of one topic that keeps its own place. Made by `Hub.subscribe`."""

    def __init__(
        self, hub: Hub, topic: str, cursor: Cursor, *, wait: float, pending: Sequence[Event] = ()
    ) -> None:
        self.hub = hub
        self.topic = topic
        self.cursor = cursor
        self.wait = wait
        self.pending = pending

    async def poll(self, *, timeout: float | None = None) -> Sequence[Event]:
        """Events since the last call. Wait up to `timeout`, or `wait` when None."""
        events = self.pending
        self.pending = ()
        if not events:
            wait = self.wait if timeout is None else timeout
            events = await self.hub.poll(self.topic, self.cursor, timeout=wait)
        if events:
            self.cursor = events[-1].id
        return events

    async def __aiter__(self) -> AsyncIterator[Event]:
        while True:
            for event in await self.poll():
                yield event


@runtime_checkable
class Hub(Protocol):
    """Application-provided event storage.

    Subclass it to have the methods checked and the missing ones refused, or
    supply any object with these five methods. Readers call `subscribe`; the
    four abstract methods are the storage it is built on.
    """

    async def subscribe(
        self, topic: str, *, after: Cursor | None = None, wait: float = 1.0
    ) -> Subscription:
        """Read after `after`, or after now. An unknown `after` raises ValueError."""
        if after is None:
            return Subscription(self, topic, await self.position(topic), wait=wait)
        found = await self.poll(topic, after, timeout=0)
        return Subscription(self, topic, after, wait=wait, pending=found)

    @abstractmethod
    async def publish(self, topic: str, message: Mapping[str, Any]) -> Cursor:
        """Append one message to `topic` and return the id it got."""

    @abstractmethod
    async def position(self, topic: str) -> Cursor:
        """Capture the cursor before triggering a publish so replies preceding the first poll are
        included.
        """

    @abstractmethod
    async def poll(self, topic: str, cursor: Cursor, *, timeout: float) -> Sequence[Event]:
        """Return the events after `cursor`, in order. Wait up to `timeout` for one.

        Raise ValueError for a cursor this hub did not issue.
        """

    @abstractmethod
    async def delete(self, topic: str) -> None:
        """Delete a completed topic."""


def topic(kind: str, name: str | None = None) -> str:
    """Prefix the topic with the current namespace to isolate callers."""
    return scoped(kind if name is None else f"{kind}/{name}")


Waiter = tuple["asyncio.AbstractEventLoop", "asyncio.Event"]


class MemoryHub(Hub):
    """Single-process hub. A topic keeps its last `keep` events for replay.

    One instance serves every thread and every event loop in the process. A
    `threading.Lock` guards the stored events, and each waiting reader holds
    an `asyncio.Event` on its own loop, which a publisher sets through
    `call_soon_threadsafe`. An `asyncio.Condition` cannot do this: it binds to
    the first loop that waits on it and refuses every other one.

    A `Subscription` keeps its own cursor and belongs to one reader. Do not
    share one between threads.
    """

    def __init__(self, *, keep: int = MEMORY_KEEP) -> None:
        self.rows: dict[str, list[Event]] = {}
        self.keep = keep
        self.last_id = 0
        #: Guards `rows`, `last_id` and `waiters`. Held for bookkeeping only,
        #: never across an await, so no reader can block a publisher for long.
        self.guard = threading.Lock()
        self.waiters: set[Waiter] = set()

    async def publish(self, topic: str, message: Mapping[str, Any]) -> Cursor:
        with self.guard:
            self.last_id += 1
            event = Event(str(self.last_id), dict(message))
            rows = self.rows.setdefault(topic, [])
            rows.append(event)
            del rows[: -self.keep]
            listening = tuple(self.waiters)
        for loop, flag in listening:
            wake(loop, flag)
        return event.id

    async def position(self, topic: str) -> Cursor:
        with self.guard:
            rows = self.rows.get(topic)
            return rows[-1].id if rows else START

    def after(self, topic: str, cursor: Cursor) -> list[Event]:
        """Events after `cursor`. Call it while holding `guard`."""
        least = int(cursor) if cursor else 0
        return [event for event in self.rows.get(topic, []) if int(event.id) > least]

    async def poll(self, topic: str, cursor: Cursor, *, timeout: float) -> Sequence[Event]:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        flag = asyncio.Event()
        waiter: Waiter = (loop, flag)
        with self.guard:
            found = self.after(topic, cursor)
            if not found:
                self.waiters.add(waiter)
        if found:
            return found
        try:
            while True:
                left = deadline - loop.time()
                if left <= 0:
                    return []
                # Clear before the read. A publish between the two sets the
                # flag again, so the wait below returns at once.
                flag.clear()
                with self.guard:
                    found = self.after(topic, cursor)
                if found:
                    return found
                with suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(flag.wait(), left)
        finally:
            with self.guard:
                self.waiters.discard(waiter)

    async def delete(self, topic: str) -> None:
        with self.guard:
            self.rows.pop(topic, None)


def wake(loop: asyncio.AbstractEventLoop, flag: asyncio.Event) -> None:
    """Set `flag` on the loop that waits on it.

    A loop that has closed takes no more callbacks. Its reader is gone, so
    the failure is the expected end of that waiter, not an error.
    """
    try:
        loop.call_soon_threadsafe(flag.set)
    except RuntimeError:
        pass


__all__ = [
    "ASK",
    "MEMORY_KEEP",
    "NOTIFICATIONS",
    "START",
    "Cursor",
    "Event",
    "Hub",
    "MemoryHub",
    "Subscription",
    "topic",
]
