"""Removing one image.

The daemon refuses where a container still holds it, which is the check worth
having; `force` overrides that and leaves the container unable to start again,
which is why the tool says so rather than offering it quietly.
"""

from __future__ import annotations

from aiodocker import Docker
from aiohttp_tiny_mcp import Exchange

from docker_mcp.consent import Policy, confirm, refusal
from docker_mcp.errors import clear
from docker_mcp.events import publish
from docker_mcp.models import ImageRef


async def remove_image(args: ImageRef, client: Docker, ex: Exchange, policy: Policy) -> str:
    """Remove an image, reclaiming its space. Asks before it does.

    Check `images` first: an image another container holds is refused unless
    `force` is set, and forcing it leaves that container unable to start
    again.
    """
    if not await confirm(
        ex,
        policy,
        "remove_image",
        args.image,
        f"Remove image {args.image}? This cannot be undone.",
    ):
        return f"not removed: {refusal(ex)}"
    async with clear("removing the image", subject=args.image):
        removed = await client.images.delete(args.image, force=args.force)
    await publish(ex.registry, "docker://images")
    return f"removed {args.image} ({len(removed)} layers untagged or deleted)"
