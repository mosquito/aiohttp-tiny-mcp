"""Images, and the only question worth asking about one on a full disk.

`used_by` is that question. It is filled from the containers, not from the
image, because the daemon does not say it: an image reports its size and its
age and leaves the caller to work out whether removing it breaks anything.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from aiodocker import Docker

from docker_mcp import daemon
from docker_mcp.models import Image, ImageFilter, Images, short


def holders(containers: Sequence[dict[str, Any]]) -> dict[str, list[str]]:
    """Which containers hold which image, by every name the image goes under.

    A container names its image as a tag, and the daemon also reports the
    digest it resolved to. Both are keys here, because a list of images is
    matched against whichever one it happens to carry.
    """
    found: dict[str, list[str]] = {}
    for raw in containers:
        name = daemon.name_of(raw)
        for key in (str(raw.get("Image", "")), str(raw.get("ImageID", ""))):
            if key:
                found.setdefault(key, []).append(name)
    return found


def image_of(raw: dict[str, Any], users: Mapping[str, list[str]] | None = None) -> Image:
    """One image, and what still holds it.

    `used_by` is the answer to the only question worth asking about an image
    on a full disk. It is empty exactly where the image can be removed.
    """
    identity = str(raw.get("Id", ""))
    tags = [tag for tag in raw.get("RepoTags") or [] if tag != "<none>:<none>"]
    holders = users or {}
    return Image(
        id=short(identity.removeprefix("sha256:")),
        tags=tags,
        size_mb=round(float(raw.get("Size", 0) or 0) / 1_000_000, 1),
        created=daemon.when(raw.get("Created")) or "",
        used_by=sorted({name for key in [identity, *tags] for name in holders.get(key, [])}),
    )


async def images(args: ImageFilter, client: Docker) -> Images:
    """List images held locally, with their sizes and what still holds them.

    `used_by` is empty exactly where an image can be removed, so ask with
    `unused` when looking for space. Check here before pulling, too.
    """
    found = await daemon.images(client)
    users = holders(await daemon.containers(client, all=True))
    listed = [image_of(raw, users) for raw in found]
    if args.reference:
        wanted = args.reference.strip("*").lower()
        listed = [item for item in listed if any(wanted in tag.lower() for tag in item.tags)]
    if args.unused:
        listed = [item for item in listed if not item.used_by]
    return Images(
        images=listed,
        total=len(listed),
        reclaimable_mb=round(sum(item.size_mb for item in listed if not item.used_by), 1),
    )
