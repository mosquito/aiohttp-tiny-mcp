"""Shared backends that let separate server processes act as one deployment.

One SQLite file is the medium, and this package's own `SqliteHub` and
`SqliteSessionStore` serve it. What is written here is the other half: the
official SDK's `SubscriptionBus` and `EventStore` over the same hub, so both
stacks are plugged into one file through each stack's own extension points.

The SDK adapters are test fixtures, not a recommendation.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from typing import Any

from aiohttp_tiny_mcp.storage.sqlite import SqliteHub, SqliteSessionStore, SqliteStorage

LOOK_AGAIN_SECONDS = 0.05

BUS_TOPIC = "sdk-subscription-bus"


def shared(path: str) -> tuple[SqliteStorage, SqliteHub, SqliteSessionStore]:
    """The three objects a node needs, on one file."""
    storage = SqliteStorage(path)
    return storage, SqliteHub(storage, look_again=LOOK_AGAIN_SECONDS), SqliteSessionStore(storage)


def event_to_row(event: Any) -> dict[str, Any]:
    """Name one SDK `ServerEvent` for storage."""
    return {"kind": type(event).__name__, "uri": getattr(event, "uri", None)}


def row_to_event(row: Mapping[str, Any]) -> Any:
    from mcp.shared.subscriptions import (
        PromptsListChanged,
        ResourcesListChanged,
        ResourceUpdated,
        ToolsListChanged,
    )

    kinds = {
        "ToolsListChanged": ToolsListChanged,
        "PromptsListChanged": PromptsListChanged,
        "ResourcesListChanged": ResourcesListChanged,
    }
    if row["kind"] == "ResourceUpdated":
        return ResourceUpdated(uri=row["uri"])
    return kinds[row["kind"]]()


class SqlSubscriptionBus:
    """The SDK's `SubscriptionBus` over the shared hub, so events cross processes.

    `publish` only appends. A poller task on every node reads the hub and calls
    the local listeners, including on the node that published.
    """

    def __init__(self, hub: SqliteHub) -> None:
        self.hub = hub
        self.listeners: dict[object, Callable[[Any], None]] = {}
        self.reader: asyncio.Task[None] | None = None

    async def publish(self, event: Any) -> None:
        await self.start()
        await self.hub.publish(BUS_TOPIC, event_to_row(event))

    def subscribe(self, listener: Callable[[Any], None]) -> Callable[[], None]:
        token = object()
        self.listeners[token] = listener
        self.reader = self.reader or asyncio.ensure_future(self.read())

        def unsubscribe() -> None:
            self.listeners.pop(token, None)

        return unsubscribe

    async def start(self) -> None:
        """Begin reading at the current end, so no earlier event is replayed."""
        if self.reader is None:
            self.reader = asyncio.ensure_future(self.read())

    async def read(self) -> None:
        async for found in await self.hub.subscribe(BUS_TOPIC, wait=3600):
            event = row_to_event(found.message)
            for listener in list(self.listeners.values()):
                listener(event)

    async def stop(self) -> None:
        if self.reader is not None:
            self.reader.cancel()
            self.reader = None


class SqlEventStore:
    """The SDK's `EventStore` on the same file, to test whether shared events alone
    make a stateful session portable between processes.
    """

    def __init__(self, hub: SqliteHub) -> None:
        self.hub = hub

    async def store_event(self, stream_id: str, message: Any) -> str:
        topic = f"sdk-stream/{stream_id}"
        body = message.model_dump(by_alias=True, mode="json") if message is not None else None
        event_id = await self.hub.publish(topic, {"stream": stream_id, "message": body})
        return f"{stream_id}@{event_id}"

    async def replay_events_after(self, last_event_id: str, send_callback: Any) -> str | None:
        from mcp.server.streamable_http import EventMessage
        from mcp_types import JSONRPCMessage

        stream_id, _, cursor = last_event_id.rpartition("@")
        topic = f"sdk-stream/{stream_id}"
        for event in await self.hub.after(topic, cursor):
            if event.message["message"] is None:
                continue
            parsed = JSONRPCMessage.model_validate(event.message["message"])
            await send_callback(EventMessage(parsed, None))
        return stream_id or None


__all__ = [
    "LOOK_AGAIN_SECONDS",
    "SqlEventStore",
    "SqlSubscriptionBus",
    "shared",
]
