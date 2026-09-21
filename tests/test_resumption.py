"""A dropped notification stream resumes where it stopped.

`2025-03-26` through `2025-11-25` put an `id` on each SSE event and replay
after `Last-Event-ID`. The id is the hub's, so any worker can replay.
`2026-07-28` has no such stream and ignores the header.
"""

from __future__ import annotations

import asyncio
import json
import uuid

import pytest
from aiohttp.test_utils import TestClient, TestServer
from pydantic import BaseModel

from aiohttp_tiny_mcp import Client, Endpoint, MemoryHub, Registry
from aiohttp_tiny_mcp.protocol.selection import AdapterSet
from aiohttp_tiny_mcp.storage.hub import NOTIFICATIONS, START, Event, topic
from aiohttp_tiny_mcp.transport.sse import read_sse

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(10)]

LEGACY = AdapterSet.default().by_version["2025-11-25"]


class Nothing(BaseModel):
    pass


@pytest.fixture
def hub(store_and_hub):
    return store_and_hub[1]


@pytest.fixture
def mine() -> str:
    return f"resume-{uuid.uuid4().hex[:12]}"


def changed(n: int) -> dict:
    return {"jsonrpc": "2.0", "method": "notifications/tools/list_changed", "params": {"n": n}}


async def test_every_event_carries_an_id_that_is_its_cursor(hub, mine):
    before = await hub.position(mine)
    for n in range(3):
        await hub.publish(mine, {"n": n})
    events = await hub.poll(mine, before, timeout=1)
    assert all(isinstance(event, Event) for event in events)
    assert len({event.id for event in events}) == 3

    followed = await hub.poll(mine, events[0].id, timeout=1)
    assert [event.message["n"] for event in followed] == [1, 2]
    assert await hub.poll(mine, events[-1].id, timeout=0.05) == []


async def test_an_id_this_hub_did_not_issue_is_refused(hub, mine):
    with pytest.raises(ValueError):
        await hub.poll(mine, "not-an-id", timeout=0.05)
    with pytest.raises(ValueError):
        await hub.subscribe(mine, after="not-an-id")


async def test_publish_returns_the_id_the_event_is_read_under(hub, mine):
    events = await hub.subscribe(mine)
    published = await hub.publish(mine, {"n": 1})
    found = await events.poll(timeout=1)
    assert [event.id for event in found] == [published]


async def test_a_subscription_starts_where_the_topic_stood_when_made(hub, mine):
    """Made before the trigger, so a reply that beats the first poll is seen."""
    await hub.publish(mine, {"n": 0})
    events = await hub.subscribe(mine)
    await hub.publish(mine, {"n": 1})
    assert [event.message["n"] for event in await events.poll(timeout=1)] == [1]
    assert await events.poll(timeout=0.05) == []
    await hub.publish(mine, {"n": 2})
    assert [event.message["n"] for event in await events.poll(timeout=1)] == [2]


async def test_a_subscription_after_an_id_replays_at_once(hub, mine):
    first = await hub.publish(mine, {"n": 1})
    await hub.publish(mine, {"n": 2})
    await hub.publish(mine, {"n": 3})
    events = await hub.subscribe(mine, after=first)
    assert [event.message["n"] for event in await events.poll(timeout=0)] == [2, 3]
    await hub.publish(mine, {"n": 4})
    assert [event.message["n"] for event in await events.poll(timeout=1)] == [4]


async def test_a_subscription_iterates(hub, mine):
    events = await hub.subscribe(mine, wait=0.05)
    for n in range(3):
        await hub.publish(mine, {"n": n})
    seen = []
    async for event in events:
        seen.append(event.message["n"])
        if len(seen) == 3:
            break
    assert seen == [0, 1, 2]


async def test_the_memory_hub_keeps_a_bounded_log():
    hub = MemoryHub(keep=3)
    for n in range(5):
        await hub.publish("demo", {"n": n})
    assert [event.message["n"] for event in await hub.poll("demo", START, timeout=0)] == [2, 3, 4]


async def test_the_stream_writes_the_hubs_id_on_each_event(registry):
    """Opened at a known id, so timing does not matter."""
    where = topic(NOTIFICATIONS)
    await registry.hub.publish(where, changed(0))
    anchor = await registry.hub.position(where)
    await registry.hub.publish(where, changed(1))
    async with TestClient(TestServer(Endpoint(registry).app("/mcp"))) as client:
        async with Client(str(client.make_url("/mcp")), LEGACY) as opened:
            await opened.initialize()
            stream = await client.get(
                "/mcp",
                headers={
                    "Accept": "text/event-stream",
                    "Mcp-Session-Id": opened.session_id or "",
                    "MCP-Protocol-Version": "2025-11-25",
                    "Last-Event-ID": anchor,
                },
            )
            event = await read_sse(stream).__anext__()
            stream.close()
    assert event.id == await registry.hub.position(where)
    assert json.loads(event.data or "")["params"] == {"n": 1}


async def test_a_reconnecting_client_is_replayed_what_it_missed(registry, real_server_url):
    where = topic(NOTIFICATIONS)
    await registry.hub.publish(where, changed(0))
    anchor = await registry.hub.position(where)
    async with Client(real_server_url, LEGACY) as client:
        await client.initialize()

        await registry.hub.publish(where, changed(1))
        first = client.stream_notifications(last_event_id=anchor)
        assert (await first.__anext__())["params"] == {"n": 1}
        await first.aclose()
        held = client.last_event_id
        assert held is not None and held != anchor

        await registry.hub.publish(where, changed(2))
        await registry.hub.publish(where, changed(3))

        again = client.stream_notifications(last_event_id=held)
        replayed = [await again.__anext__(), await again.__anext__()]
        await registry.hub.publish(where, changed(4))
        live = await again.__anext__()
        await again.aclose()

    assert [frame["params"]["n"] for frame in replayed] == [2, 3]
    assert live["params"] == {"n": 4}


class Watched(MemoryHub):
    """Signals when the server has taken its starting position."""

    def __init__(self) -> None:
        super().__init__()
        self.positioned = asyncio.Event()

    async def position(self, topic: str) -> str:
        found = await super().position(topic)
        self.positioned.set()
        return found


async def test_an_id_the_server_never_issued_starts_the_stream_from_now(real_endpoint):
    hub = Watched()
    registry = Registry("resume", "1.0", hub=hub)

    @registry.tool
    async def noop(args: Nothing) -> str:
        """Makes the server advertise tool list changes."""
        return ""

    url = await real_endpoint(registry)
    where = topic(NOTIFICATIONS)
    async with Client(url, LEGACY) as client:
        await client.initialize()
        await registry.hub.publish(where, changed(1))

        stream = client.stream_notifications(last_event_id="never-issued")
        reading = asyncio.ensure_future(stream.__anext__())
        await hub.positioned.wait()
        await registry.hub.publish(where, changed(2))
        frame = await reading
        await stream.aclose()

    assert frame["params"] == {"n": 2}


async def test_the_latest_revision_has_no_stream_to_resume(registry, real_server_url):
    """`2026-07-28` has no GET stream and ignores `Last-Event-ID`."""
    modern = AdapterSet.default().by_version["2026-07-28"]
    async with Client(real_server_url, modern) as client:
        assert client.session is not None
        answered = await client.session.get(
            real_server_url,
            headers={
                "Accept": "text/event-stream",
                "MCP-Protocol-Version": "2026-07-28",
                "Last-Event-ID": "1",
            },
        )
        assert answered.status == 405
        answered.close()
