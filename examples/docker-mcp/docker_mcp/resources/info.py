"""Daemon information exposed as a resource."""

from __future__ import annotations

from aiodocker import Docker

from docker_mcp import tools
from docker_mcp.models import Info, Nothing


async def info(args: Nothing, client: Docker) -> Info:
    """The daemon's version and how much it holds."""
    return await tools.info(args, client)
