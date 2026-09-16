"""Downloading an image, saying so as it goes.

Minutes of silence is indistinguishable from a hang. The layers are counted
rather than their bytes, because the daemon reports progress per layer and a
total it never states cannot be invented here.
"""

from __future__ import annotations

from aiodocker import Docker
from aiohttp_tiny_mcp import Exchange

from docker_mcp.events import publish
from docker_mcp.models import PullRequest


async def pull(args: PullRequest, client: Docker, ex: Exchange) -> str:
    """Download an image, reporting progress as it goes.

    This can take minutes for a large image. Check `images` first.
    """
    await ex.log("info", f"pulling {args.image}", logger="pull")
    layers: dict[str, str] = {}
    async for line in client.images.pull(args.image, stream=True):
        layer = str(line.get("id", ""))
        status = str(line.get("status", ""))
        if layer:
            layers[layer] = status
        done = sum(1 for value in layers.values() if "complete" in value.lower())
        await ex.progress(done, max(len(layers), 1), message=status or "working")
        if "error" in line:
            await ex.log("error", str(line["error"]), logger="pull")
            return f"failed: {line['error']}"
    await ex.log("info", f"pulled {args.image}", logger="pull")
    await publish(ex.registry, "docker://images")
    return f"pulled {args.image} ({len(layers)} layers)"
