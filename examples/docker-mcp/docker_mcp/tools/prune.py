"""Removing everything of one kind that nothing uses.

The tool for a full disk, and the one that removes the most at once. Each kind
is a separate call rather than a single sweep, so the question names what will
go and a person can agree to one kind without agreeing to all four.
"""

from __future__ import annotations

from aiodocker import Docker
from aiohttp_tiny_mcp import Exchange

from docker_mcp.consent import Policy, confirm, refusal
from docker_mcp.errors import Conflict, clear
from docker_mcp.events import publish
from docker_mcp.models import Pruned, PruneRequest


async def prune(args: PruneRequest, client: Docker, ex: Exchange, policy: Policy) -> Pruned:
    """Remove everything of one kind that nothing uses. Asks before it does.

    This is the tool for a full disk. It is also the one that removes the most
    at once, so the question names what will go. Stopped containers, untagged
    images, unattached volumes and empty networks are each a separate call.
    """
    filters = {"dangling": ["true"]} if args.what == "images" and args.dangling_only else None
    if not await confirm(
        ex,
        policy,
        "prune",
        args.what,
        f"Remove every unused {args.what.rstrip('s')}? This cannot be undone.",
    ):
        raise Conflict(f"nothing removed: {refusal(ex)}")
    kinds = {
        "containers": client.containers,
        "images": client.images,
        "volumes": client.volumes,
        "networks": client.networks,
    }
    async with clear(f"removing unused {args.what}"):
        reply = dict(await kinds[args.what].prune(filters=filters))
    removed = [
        str(item)
        for key in ("ContainersDeleted", "ImagesDeleted", "VolumesDeleted", "NetworksDeleted")
        for item in reply.get(key) or []
    ]
    await publish(ex.registry, f"docker://{args.what}")
    return Pruned(
        what=args.what,
        removed=[item for item in removed if item],
        reclaimed_mb=round(float(reply.get("SpaceReclaimed", 0) or 0) / 1_000_000, 1),
    )
