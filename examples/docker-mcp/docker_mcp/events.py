"""Relay Docker events as resource notifications."""

from __future__ import annotations

import asyncio
import logging

from aiodocker import Docker
from aiodocker.exceptions import DockerError
from aiohttp_tiny_mcp import Registry
from aiohttp_tiny_mcp.hub import NOTIFICATIONS, topic

log = logging.getLogger(__name__)


async def publish(registry: Registry, uri: str) -> None:
    await registry.hub.publish(
        topic(NOTIFICATIONS),
        {
            "jsonrpc": "2.0",
            "method": "notifications/resources/updated",
            "params": {"uri": uri},
        },
    )


async def relay(registry: Registry, client: Docker) -> None:
    """Publish one notification per container event. Runs until cancelled."""
    subscriber = client.events.subscribe()
    while True:
        event = await subscriber.get()
        if event is None:
            return
        if event.get("Type") != "container":
            continue
        await publish(registry, "docker://containers")
        identity = (event.get("Actor") or {}).get("ID")
        if identity:
            await publish(registry, f"docker://containers/{identity[:12]}")


async def watch(registry: Registry, client: Docker) -> None:
    """Reconnect the event relay when the daemon disconnects."""
    while True:
        try:
            await relay(registry, client)
        except asyncio.CancelledError:
            raise
        except (DockerError, OSError):
            log.warning("event stream lost, reconnecting", exc_info=True)
        await asyncio.sleep(1)
