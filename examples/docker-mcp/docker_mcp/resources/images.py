"""`docker://images` -- every image held locally."""

from __future__ import annotations

from aiodocker import Docker

from docker_mcp import tools
from docker_mcp.models import ImageFilter, Images, Nothing


async def images(args: Nothing, client: Docker) -> Images:
    """Every image held locally."""
    return await tools.images(ImageFilter(), client)
