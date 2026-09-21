"""Cursor-based event storage shared by reply and notification streams.

Applications supply the backend; MemoryHub serves one process. Shared storage lets one node
publish replies that another is awaiting.
"""

from __future__ import annotations

import asyncio
from abc import abstractmethod
from collections.abc import AsyncIterator, Mapping, Sequence
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


class MemoryHub(Hub):
    """Single-process hub. A topic keeps its last `keep` events for replay."""

    def __init__(self, *, keep: int = MEMORY_KEEP) -> None:
        self.rows: dict[str, list[Event]] = {}
        self.keep = keep
        self.last_id = 0
        self.arrived = asyncio.Condition()

    async def publish(self, topic: str, message: Mapping[str, Any]) -> Cursor:
        async with self.arrived:
            self.last_id += 1
            event = Event(str(self.last_id), dict(message))
            rows = self.rows.setdefault(topic, [])
            rows.append(event)
            del rows[: -self.keep]
            self.arrived.notify_all()
        return event.id

    async def position(self, topic: str) -> Cursor:
        rows = self.rows.get(topic)
        return rows[-1].id if rows else START

    def after(self, topic: str, cursor: Cursor) -> list[Event]:
        least = int(cursor) if cursor else 0
        return [event for event in self.rows.get(topic, []) if int(event.id) > least]

    async def poll(self, topic: str, cursor: Cursor, *, timeout: float) -> Sequence[Event]:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        async with self.arrived:
            while True:
                found = self.after(topic, cursor)
                if found:
                    return found
                left = deadline - loop.time()
                if left <= 0:
                    return []
                try:
                    await asyncio.wait_for(self.arrived.wait(), left)
                except asyncio.TimeoutError:
                    return []

    async def delete(self, topic: str) -> None:
        async with self.arrived:
            self.rows.pop(topic, None)


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
