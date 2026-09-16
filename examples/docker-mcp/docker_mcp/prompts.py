"""Diagnostic prompts and completion for current container and image names."""

from __future__ import annotations

from aiodocker import Docker
from aiohttp_tiny_mcp.models import CompleteParams

from docker_mcp import daemon

from .models import ContainerRef, Nothing


async def diagnose(args: ContainerRef) -> str:
    """Work out why a container is unhealthy or keeps restarting."""
    return (
        f"Container {args.container} is not behaving. Work out why.\n\n"
        "Start with `container` for its state, exit code, health and restart "
        "count. Read `logs` for what it wrote before it stopped, and `events` "
        "for whether this has happened before and how often. Where it is a "
        "resource problem, `stats` says so; where it is a configuration "
        "problem, `read` shows the file the program actually has. Check "
        "whether the exit code and the last lines agree about the cause.\n\n"
        "Report the cause, the evidence for it, and the smallest change that "
        "would fix it. Say so plainly if the evidence does not settle it."
    )


async def reclaim(args: Nothing) -> str:
    """Work out what can safely be removed."""
    return (
        "Find what is taking space and what can go.\n\n"
        "Ask `images` with `unused` for what no container holds, and read "
        "`reclaimable_mb` for what that is worth. Ask `volumes` for those "
        "nothing mounts, and `containers` with `all` for the stopped ones. A "
        "volume nobody mounts is not always waste: read its name before "
        "proposing it, because it may be a database whose container is "
        "gone.\n\n"
        "Propose what to remove and why, largest first, and name the tool for "
        "each: `prune` for a whole kind at once, `remove_image` for one. Do "
        "not remove anything: say what you would remove and let the person "
        "decide."
    )


async def complete(args: CompleteParams, client: Docker) -> list[str]:
    """Read current container and image names for completion."""
    typed = args.argument.value.lower()
    if args.argument.name in ("container", "name"):
        names = [
            str(name).lstrip("/")
            for raw in await daemon.containers(client, all=True)
            for name in raw.get("Names") or []
        ]
    elif args.argument.name == "image":
        names = [tag for raw in await daemon.images(client) for tag in raw.get("RepoTags") or []]
    else:
        return []
    return sorted({name for name in names if typed in name.lower()})[:100]
