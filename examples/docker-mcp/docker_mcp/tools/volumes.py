"""Volumes, and what mounts them.

An empty `used_by` is not the same as waste. It is either space to reclaim or
a database whose container is gone, and only the name tells the two apart --
which is why this reports and never removes.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from aiodocker import Docker

from docker_mcp import daemon
from docker_mcp.errors import clear
from docker_mcp.models import NameFilter, Volume, Volumes


def mounters(containers: Sequence[dict[str, Any]]) -> dict[str, list[str]]:
    """Which containers mount which named volume."""
    found: dict[str, list[str]] = {}
    for raw in containers:
        name = daemon.name_of(raw)
        for mount in raw.get("Mounts") or []:
            if mount.get("Type") == "volume" and mount.get("Name"):
                found.setdefault(str(mount["Name"]), []).append(name)
    return found


def volume_of(raw: dict[str, Any], users: Mapping[str, list[str]] | None = None) -> Volume:
    name = str(raw.get("Name", ""))
    return Volume(
        name=name,
        driver=str(raw.get("Driver", "")),
        mountpoint=str(raw.get("Mountpoint", "")),
        created=daemon.when(raw.get("CreatedAt")),
        used_by=sorted((users or {}).get(name, [])),
    )


async def volumes(args: NameFilter, client: Docker) -> Volumes:
    """List volumes, and which containers mount them.

    `used_by` is empty where a volume holds data nothing is reading -- which
    is either space to reclaim or a service that lost its data, so read the
    name before removing it.
    """
    users = mounters(await daemon.containers(client, all=True))
    async with clear("listing volumes"):
        found = await client.volumes.list()
    listed = [volume_of(dict(raw), users) for raw in found.get("Volumes") or []]
    if args.name:
        wanted = args.name.lower()
        listed = [item for item in listed if wanted in item.name.lower()]
    return Volumes(volumes=listed, total=len(listed))
