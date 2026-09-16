"""All containers as context, including stopped containers."""

from __future__ import annotations

from aiodocker import Docker

from docker_mcp import tools
from docker_mcp.models import ContainerFilter, Containers, Nothing


async def containers(args: Nothing, client: Docker) -> Containers:
    """Every container, running or not."""
    return await tools.containers(ContainerFilter(all=True, limit=500), client)
