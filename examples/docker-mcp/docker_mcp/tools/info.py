"""Which daemon this is, before anything is blamed on it.

`endpoint` is here for one failure that costs an afternoon: a server pointed
at the wrong machine looks exactly like a machine with nothing on it.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from aiodocker import Docker

from docker_mcp import daemon
from docker_mcp.errors import clear
from docker_mcp.models import Info, Nothing


def info_of(
    endpoint: str, version: dict[str, Any], containers: Sequence[dict[str, Any]], images: int
) -> Info:
    running = sum(1 for item in containers if item.get("State") == "running")
    return Info(
        endpoint=endpoint,
        version=str(version.get("Version", "")),
        api_version=str(version.get("ApiVersion", "")),
        os=str(version.get("Os", "")),
        architecture=str(version.get("Arch", "")),
        containers_running=running,
        containers_total=len(containers),
        images=images,
    )


async def version(client: Docker) -> dict[str, Any]:
    async with clear("reading the daemon's version"):
        return dict(await client.version())


async def info(args: Nothing, client: Docker) -> Info:
    """The daemon itself: where it is, its version, and how much it holds.

    Use this to confirm Docker is reachable before blaming anything else, and
    to confirm it is the machine you meant.
    """
    return info_of(
        str(client.docker_host),
        await version(client),
        await daemon.containers(client, all=True),
        len(await daemon.images(client)),
    )
