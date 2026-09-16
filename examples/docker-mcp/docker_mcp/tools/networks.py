"""Networks, for the question no container's own state answers.

Two containers that cannot reach each other are usually not on one network,
and nothing in either container says so.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from aiodocker import Docker

from docker_mcp import daemon
from docker_mcp.errors import clear
from docker_mcp.models import NameFilter, Network, Networks, short


def attachments(containers: Sequence[dict[str, Any]]) -> dict[str, list[str]]:
    """Which containers are on which network.

    Built from the containers, because the daemon will not say. `GET /networks`
    omits `Containers` -- only inspecting one network reports it -- so reading
    it from the list gives an answer that is always empty.
    """
    found: dict[str, list[str]] = {}
    for raw in containers:
        name = daemon.name_of(raw)
        for network in (raw.get("NetworkSettings") or {}).get("Networks") or {}:
            found.setdefault(str(network), []).append(name)
    return found


def network_of(raw: dict[str, Any], attached: Mapping[str, list[str]] | None = None) -> Network:
    config = (raw.get("IPAM") or {}).get("Config") or []
    return Network(
        id=short(str(raw.get("Id", ""))),
        name=str(raw.get("Name", "")),
        driver=str(raw.get("Driver", "")),
        scope=str(raw.get("Scope", "")),
        subnets=[str(item.get("Subnet")) for item in config if item.get("Subnet")],
        containers=sorted((attached or {}).get(str(raw.get("Name", "")), [])),
    )


async def networks(args: NameFilter, client: Docker) -> Networks:
    """List networks, with their subnets and what is attached.

    Use this when two containers cannot reach each other: the usual cause is
    that they are not on one network.
    """
    attached = attachments(await daemon.containers(client, all=True))
    async with clear("listing networks"):
        found = await client.networks.list()
    listed = [network_of(dict(raw), attached) for raw in found]
    if args.name:
        wanted = args.name.lower()
        listed = [item for item in listed if wanted in item.name.lower()]
    return Networks(networks=listed, total=len(listed))
