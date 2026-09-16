"""`docker://containers/{name}` -- one container in full."""

from __future__ import annotations

from aiodocker import Docker

from docker_mcp import tools
from docker_mcp.models import ContainerDetail, ContainerName, ContainerRef


async def container(args: ContainerName, client: Docker) -> ContainerDetail:
    """One container in full."""
    return await tools.container(ContainerRef(container=args.name), client)
