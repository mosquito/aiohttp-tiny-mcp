"""A plain-text log attachment, limited to the last 200 lines."""

from __future__ import annotations

from aiodocker import Docker

from docker_mcp import tools
from docker_mcp.models import ContainerName, LogRequest


async def logs(args: ContainerName, client: Docker) -> str:
    """The last two hundred lines one container wrote."""
    written = await tools.logs(LogRequest(container=args.name, lines=200), client)
    return "\n".join(written.lines)
