"""Where every other tool starts, so the row is short on purpose.

Name, image, state and ports answer "which one", which is all this is for.
Everything else is one more call away, and putting it here would make the
common case pay for the rare one.
"""

from __future__ import annotations

from typing import Any

from aiodocker import Docker

from docker_mcp import daemon
from docker_mcp.models import Container, ContainerFilter, Containers, port_list, short


def summary(raw: dict[str, Any]) -> Container:
    return Container(
        id=short(str(raw.get("Id", ""))),
        name=daemon.name_of(raw),
        image=str(raw.get("Image", "")),
        state=str(raw.get("State", "unknown")),
        status=str(raw.get("Status", "")),
        ports=port_list(raw.get("Ports")),
    )


async def containers(args: ContainerFilter, client: Docker) -> Containers:
    """List containers. Use this first to find what to act on.

    Reports name, image, state and published ports. Running only unless `all`
    is true. Ask for `container` when one of these needs explaining.
    """
    found = await daemon.containers(client, all=args.all)
    if args.name:
        wanted = args.name.lower()
        found = [
            raw
            for raw in found
            if any(wanted in str(name).lower() for name in raw.get("Names") or [])
        ]
    shown = [summary(raw) for raw in found[: args.limit]]
    return Containers(containers=shown, shown=len(shown), total=len(found))
