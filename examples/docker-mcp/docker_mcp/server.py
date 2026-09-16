"""Register Docker handlers, descriptions, and annotations."""

from __future__ import annotations

from aiodocker import Docker
from aiohttp_tiny_mcp import Hint, Registry

from . import prompts, resources, tools
from .consent import Policy
from .resources import templates

INSTRUCTIONS = (
    "Inspect and control Docker. Start with `containers` to find what to act "
    "on; `container`, `logs`, `read` and `stats` to understand one; `events` "
    "for what happened before now; and `info` to confirm the daemon is "
    "reachable and is the one you meant.\n\n"
    "To change something: `create` makes and starts a container, `start`, "
    "`stop` and `restart` do the obvious, `wait` holds until a container is "
    "running or healthy, `exec` runs a command inside one, and `write` puts a "
    "file into one. `prune`, `remove` and `remove_image` reclaim space.\n\n"
    "Reading never asks, and neither does a command that only reads, such as "
    "`ls` or `cat`. What cannot be undone -- `remove`, `remove_image`, "
    "`prune`, and a command that is not merely reading -- asks once, and the "
    "person answering may say not to ask again."
)

READS = Hint.READ_ONLY | Hint.IDEMPOTENT

WATCHES = Hint.READ_ONLY


def build(registry: Registry, client: Docker, policy: Policy | None = None) -> Registry:
    """Register handlers and inject the Docker client and deployment confirmation policy."""
    registry.instructions = INSTRUCTIONS
    registry.provide_instance(client)
    registry.provide_instance(policy or Policy())

    registry.tool(tools.containers, annotations=READS)
    registry.tool(tools.container, annotations=READS)
    registry.tool(tools.read, annotations=READS)
    registry.tool(tools.images, annotations=READS)
    registry.tool(tools.networks, annotations=READS)
    registry.tool(tools.volumes, annotations=READS)
    registry.tool(tools.info, annotations=READS)
    registry.tool(tools.logs, annotations=WATCHES)
    registry.tool(tools.stats, annotations=WATCHES)
    registry.tool(tools.events, annotations=WATCHES)
    registry.tool(tools.wait, annotations=WATCHES)

    registry.tool(tools.create, annotations=Hint.OPEN_WORLD)
    registry.tool(tools.start, annotations=Hint.IDEMPOTENT)
    registry.tool(tools.stop, annotations=Hint.IDEMPOTENT)
    registry.tool(tools.restart)
    registry.tool(tools.write, annotations=Hint.DESTRUCTIVE)
    registry.tool(tools.exec, annotations=Hint.DESTRUCTIVE | Hint.OPEN_WORLD)
    registry.tool(tools.remove, annotations=Hint.DESTRUCTIVE)
    registry.tool(tools.remove_image, annotations=Hint.DESTRUCTIVE)
    registry.tool(tools.prune, annotations=Hint.DESTRUCTIVE)
    registry.tool(tools.pull, streaming=True, annotations=Hint.OPEN_WORLD)

    registry.resource("docker://info", resources.info, mime_type="application/json")
    registry.resource("docker://containers", resources.containers, mime_type="application/json")
    registry.resource(
        "docker://containers/{name}",
        templates.container,
        name="container",
        mime_type="application/json",
    )
    registry.resource("docker://containers/{name}/logs", templates.logs, name="container-logs")
    registry.resource("docker://images", resources.images, mime_type="application/json")

    registry.prompt(prompts.diagnose, title="Diagnose a container")
    registry.prompt(prompts.reclaim, title="Find space to reclaim")

    registry.completions(prompts.complete)
    return registry


__all__ = ["INSTRUCTIONS", "build"]
