"""Stopping and starting, which is the usual first thing to try.

Like `start`, it reports the state a moment later, because the container that
comes back up is not always the one that went down.
"""

from __future__ import annotations

from aiodocker import Docker
from aiohttp_tiny_mcp import Exchange

from docker_mcp import daemon
from docker_mcp.events import publish
from docker_mcp.models import Done, StopRequest
from docker_mcp.safety import checked_container

from .results import outcome, settled


async def restart(args: StopRequest, client: Docker, ex: Exchange) -> Done:
    """Stop and start a container. The usual first thing to try."""
    found = await daemon.find(client, args.container)
    await found.restart(timeout=args.seconds)
    await publish(ex.registry, "docker://containers")
    return outcome(args.container, "restart", await settled(found), "running")


async def restart_unprivileged(args: StopRequest, client: Docker, ex: Exchange) -> Done:
    """Restart a container after checking that it has no host access."""
    identity = await checked_container(client, args.container)
    return await restart(args.model_copy(update={"container": identity}), client, ex)
