"""A task whose first cancellation is lost still ends when the server lets go of it.

Before Python 3.12, `asyncio.wait_for` drops a cancellation that arrives in
the loop turn its inner future completes; psycopg_pool waits for a
connection that way. The streams here must not depend on a cancellation
being honoured the first time.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from pydantic import BaseModel

from aiohttp_tiny_mcp import Endpoint, MemoryHub, MemorySessionStore, Registry, SseEndpoint
from aiohttp_tiny_mcp.sse import read_sse
from aiohttp_tiny_mcp.tasks import stop

pytestmark = pytest.mark.timeout(15)


class Stubborn(MemoryHub):
    """A hub whose poll swallows the first cancellation of every task, as a
    library racing `asyncio.wait_for` would."""

    def __init__(self) -> None:
        super().__init__()
        self.swallowed: set[str] = set()

    async def poll(self, topic, cursor, *, timeout):
        try:
            return await super().poll(topic, cursor, timeout=timeout)
        except asyncio.CancelledError:
            name = asyncio.current_task().get_name()  # type: ignore[union-attr]
            if name in self.swallowed:
                raise
            self.swallowed.add(name)
            return []


class Nothing(BaseModel):
    pass


def registry_on(hub: MemoryHub) -> Registry:
    registry = Registry("stubborn", "1.0", hub=hub, session_store=MemorySessionStore())

    @registry.tool
    async def noop(args: Nothing) -> str:
        """Makes the server advertise tool list changes."""
        return ""

    return registry


async def test_stop_cancels_again_until_the_task_ends():
    lost = 0

    async def stubborn() -> None:
        nonlocal lost
        while True:
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                if lost:
                    raise
                lost += 1  # the first cancellation goes missing

    task = asyncio.create_task(stubborn())
    await asyncio.sleep(0)
    await asyncio.wait_for(stop(task, grace=0.05), 2)
    assert task.cancelled()
    assert lost == 1


async def test_stop_reports_a_failure_and_ignores_a_lost_connection():
    async def failing() -> None:
        raise RuntimeError("broken")

    async def dropped() -> None:
        raise ConnectionResetError

    ended = [asyncio.create_task(failing()), asyncio.create_task(dropped())]
    await asyncio.wait(ended)  # let them fail before they are stopped
    with pytest.raises(RuntimeError, match="broken"):
        await stop(*ended)
    lost = asyncio.create_task(dropped())
    await asyncio.wait({lost})
    await stop(lost)


async def test_stop_returns_at_once_for_tasks_that_ended():
    done = asyncio.create_task(asyncio.sleep(0))
    await done
    await stop(done)


async def test_the_old_transport_lets_go_of_a_stream_whose_relay_lost_its_cancel():
    hub = Stubborn()
    app = web.Application()
    SseEndpoint(registry_on(hub)).setup(app)
    client = TestClient(TestServer(app))
    await client.start_server()
    stream = await client.get("/sse", headers={"Accept": "text/event-stream"})
    event = await asyncio.wait_for(read_sse(stream).__anext__(), 5)
    assert event.event == "endpoint"
    stream.close()
    await asyncio.wait_for(client.close(), 5)
    assert hub.swallowed, "the hub did not get to swallow anything, so nothing was proven"


async def test_the_get_stream_lets_go_when_its_relay_lost_its_cancel():
    hub = Stubborn()
    registry = registry_on(hub)
    client = TestClient(TestServer(Endpoint(registry).app("/mcp")))
    await client.start_server()
    opened = await client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2025-11-25", "capabilities": {}},
        },
        headers={"Accept": "application/json, text/event-stream"},
    )
    session = opened.headers["Mcp-Session-Id"]
    # Published before the stream opens and replayed from the anchor, so the
    # event cannot be missed and the relay is known to have polled once.
    anchor = await hub.publish("notifications", {"jsonrpc": "2.0", "method": "notifications/x"})
    await hub.publish(
        "notifications", {"jsonrpc": "2.0", "method": "notifications/tools/list_changed"}
    )
    stream = await client.get(
        "/mcp",
        headers={
            "Accept": "text/event-stream",
            "Mcp-Session-Id": session,
            "MCP-Protocol-Version": "2025-11-25",
            "Last-Event-ID": anchor,
        },
    )
    assert stream.status == 200
    event = await asyncio.wait_for(read_sse(stream).__anext__(), 5)
    assert json.loads(event.data or "")["method"] == "notifications/tools/list_changed"
    stream.close()
    await asyncio.wait_for(client.close(), 5)
    assert hub.swallowed
