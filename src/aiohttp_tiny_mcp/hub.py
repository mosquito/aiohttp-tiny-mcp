"""Cursor-based event storage shared by reply and notification streams.

Applications supply the backend; MemoryHub serves one process. Shared storage lets one node
publish replies that another is awaiting.
"""

from __future__ import annotations

import asyncio
from abc import abstractmethod
from collections.abc import Mapping, Sequence
from typing import Any, Protocol, runtime_checkable

from .namespaces import scoped

Cursor = str

START: Cursor = ""

NOTIFICATIONS = "notifications"

ASK = "ask"


@runtime_checkable
class Hub(Protocol):
    """Application-provided event storage.

    Subclass it to have the methods checked and the missing ones refused, or
    supply any object with these four methods: this is a protocol, so a
    backend that inherits nothing is still a Hub.
    """

    @abstractmethod
    async def publish(self, topic: str, message: Mapping[str, Any]) -> None:
        """Append one message to `topic`."""

    @abstractmethod
    async def position(self, topic: str) -> Cursor:
        """Capture the cursor before triggering a publish so replies preceding the first poll are
        included.
        """

    @abstractmethod
    async def poll(
        self, topic: str, cursor: Cursor, *, timeout: float
    ) -> tuple[Sequence[Mapping[str, Any]], Cursor]:
        """Return messages after cursor and the next cursor.

        Wait up to timeout seconds for a message. Backends choose whether to poll or wait for
        notification; timeout is a deadline, not a polling interval.
        """

    @abstractmethod
    async def delete(self, topic: str) -> None:
        """Delete a completed topic."""


def topic(kind: str, name: str | None = None) -> str:
    """Prefix the topic with the current namespace to isolate callers."""
    return scoped(kind if name is None else f"{kind}/{name}")


class MemoryHub(Hub):
    """Single-process hub, waiting on a condition rather than sleeping."""

    def __init__(self) -> None:
        self.rows: dict[str, list[tuple[int, Mapping[str, Any]]]] = {}
        self.last_id = 0
        self.arrived = asyncio.Condition()

    async def publish(self, topic: str, message: Mapping[str, Any]) -> None:
        async with self.arrived:
            self.last_id += 1
            self.rows.setdefault(topic, []).append((self.last_id, dict(message)))
            self.arrived.notify_all()

    async def position(self, topic: str) -> Cursor:
        rows = self.rows.get(topic)
        return str(rows[-1][0]) if rows else START

    def after(self, topic: str, cursor: Cursor) -> list[tuple[int, Mapping[str, Any]]]:
        least = int(cursor) if cursor else 0
        return [row for row in self.rows.get(topic, []) if row[0] > least]

    async def poll(
        self, topic: str, cursor: Cursor, *, timeout: float
    ) -> tuple[Sequence[Mapping[str, Any]], Cursor]:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        async with self.arrived:
            while True:
                found = self.after(topic, cursor)
                if found:
                    return [message for _, message in found], str(found[-1][0])
                left = deadline - loop.time()
                if left <= 0:
                    return [], cursor
                try:
                    await asyncio.wait_for(self.arrived.wait(), left)
                except asyncio.TimeoutError:
                    return [], cursor

    async def delete(self, topic: str) -> None:
        async with self.arrived:
            self.rows.pop(topic, None)


__all__ = ["ASK", "NOTIFICATIONS", "START", "Cursor", "Hub", "MemoryHub", "topic"]
