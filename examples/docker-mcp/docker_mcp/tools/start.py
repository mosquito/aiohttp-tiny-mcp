"""Starting a container, and reporting what became of it.

Docker answers before the process inside is up, so the state is read a moment
later. A container that starts and dies at once is reported as dead, with its
exit code, instead of being reported as started.
"""

from __future__ import annotations

from aiodocker import Docker
from aiohttp_tiny_mcp import Exchange

from docker_mcp import daemon
from docker_mcp.events import publish
from docker_mcp.models import ContainerRef, Done

from .results import outcome, settled


async def start(args: ContainerRef, client: Docker, ex: Exchange) -> Done:
    """Start a stopped container. Starting a running one changes nothing.

    Reports the state a moment later, so a container that starts and dies at
    once is reported as dead rather than as started.
    """
    found = await daemon.find(client, args.container)
    await found.start()
    await publish(ex.registry, "docker://containers")
    return outcome(args.container, "start", await settled(found), "running")
