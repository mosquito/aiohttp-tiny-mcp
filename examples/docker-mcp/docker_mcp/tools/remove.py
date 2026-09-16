"""Removing a container for good.

The first of the three that cannot be undone, and so one of the few the
default policy still asks about. The removal itself comes after the question,
because everything before it runs again when the answer arrives.
"""

from __future__ import annotations

from aiodocker import Docker
from aiohttp_tiny_mcp import Exchange

from docker_mcp import daemon
from docker_mcp.consent import Policy, confirm, refusal
from docker_mcp.events import publish
from docker_mcp.models import RemoveRequest


async def remove(args: RemoveRequest, client: Docker, ex: Exchange, policy: Policy) -> str:
    """Remove a container for good. Asks before it does.

    This cannot be undone. Stop it first unless `force` is set; a running
    container is not removed by accident.
    """
    found = await daemon.find(client, args.container)
    if not await confirm(
        ex,
        policy,
        "remove",
        args.container,
        f"Remove container {args.container}? This cannot be undone.",
    ):
        return f"not removed: {refusal(ex)}"
    await found.delete(force=args.force, v=args.volumes)
    await publish(ex.registry, "docker://containers")
    return f"removed {args.container}"
