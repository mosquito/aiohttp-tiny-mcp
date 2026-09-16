"""Docker events relayed through the hub to subscribed MCP clients."""

from __future__ import annotations

import asyncio

import pytest
from aiohttp_tiny_mcp import Client
from aiohttp_tiny_mcp.hub import NOTIFICATIONS, topic
from aiohttp_tiny_mcp.protocol.selection import AdapterSet
from fake_exchange import FakeExchange

from docker_mcp.consent import Policy
from docker_mcp.events import relay
from docker_mcp.models import RemoveRequest
from docker_mcp.tools import remove

pytestmark = pytest.mark.asyncio

EVERY_REVISION = AdapterSet.default().adapters


async def first_change(stream):
    async for change in stream:
        return change
    return None


@pytest.mark.parametrize("adapter", EVERY_REVISION, ids=lambda a: a.version)
async def test_a_daemon_event_reaches_a_subscriber(url, registry, client, adapter):
    """Let the relay publish the daemon event through the production notification path."""
    async with Client(url, adapter) as subscriber:
        await subscriber.initialize()
        stream = subscriber.listen(resources=["docker://containers"])
        heard = asyncio.ensure_future(first_change(stream))
        await asyncio.sleep(0.05)

        watching = asyncio.ensure_future(relay(registry, client))
        try:
            change = await asyncio.wait_for(asyncio.shield(heard), 5)
        finally:
            watching.cancel()
            await asyncio.gather(watching, return_exceptions=True)
            await stream.aclose()

    assert change["method"] == "notifications/resources/updated"
    assert change["params"]["uri"] == "docker://containers"


async def test_the_relay_names_the_container_that_changed(registry, client):
    where = topic(NOTIFICATIONS)
    cursor = await registry.hub.position(where)
    watching = asyncio.ensure_future(relay(registry, client))
    try:
        messages, _ = await registry.hub.poll(where, cursor, timeout=5)
    finally:
        watching.cancel()
        await asyncio.gather(watching, return_exceptions=True)

    uris = {message["params"]["uri"] for message in messages}
    assert "docker://containers" in uris
    assert "docker://containers/333333333333" in uris


async def test_a_change_this_server_made_is_announced_too(client):
    ex = FakeExchange(answers={"confirm": {"confirmed": True}})
    await remove(RemoveRequest(container="worker"), client, ex, Policy())

    published = [message["params"]["uri"] for _, message in ex.registry.hub.published]
    assert published == ["docker://containers"]
