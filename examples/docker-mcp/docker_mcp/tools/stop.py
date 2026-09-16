"""Stopping a container, with time to finish first.

The state is read straight after rather than after a pause: `stop` already
waited, and there is nothing left to settle.
"""

from __future__ import annotations

from aiodocker import Docker
from aiohttp_tiny_mcp import Exchange

from docker_mcp import daemon
from docker_mcp.events import publish
from docker_mcp.models import Done, StopRequest

from .results import outcome


async def stop(args: StopRequest, client: Docker, ex: Exchange) -> Done:
    """Stop a running container, giving it `seconds` to finish first.

    It is killed if it has not stopped by then. Stopping a stopped container
    changes nothing.
    """
    found = await daemon.find(client, args.container)
    await found.stop(t=args.seconds)
    await publish(ex.registry, "docker://containers")
    return outcome(args.container, "stop", await daemon.state_of(found), "exited")
