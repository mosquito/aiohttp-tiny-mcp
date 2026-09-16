"""What a change achieved, rather than what it was asked to do."""

from __future__ import annotations

import asyncio
from typing import Any

from aiodocker.containers import DockerContainer

from docker_mcp import daemon
from docker_mcp.models import Done

SETTLE_SECONDS = 0.4


async def settled(container: DockerContainer) -> dict[str, Any]:
    """Read the state a moment after a change, rather than during it."""
    await asyncio.sleep(SETTLE_SECONDS)
    return await daemon.state_of(container)


def outcome(name: str, action: str, state: dict[str, Any], wanted: str) -> Done:
    """Report what a change achieved, not only that it was asked for.

    A container that starts and dies within the second is the case this exists
    for. Reporting `state` alone is true and useless: the caller then spends
    two more calls learning what this already knows.
    """
    status = str(state.get("Status", "unknown"))
    health = (state.get("Health") or {}).get("Status")
    code = state.get("ExitCode")
    exit_code = int(code) if code is not None and status != "running" else None
    note = None
    if wanted == "running" and status != "running":
        note = f"it did not stay running: it is {status}"
        if exit_code is not None:
            note += f", having exited with {exit_code}. Read `logs` for why"
    elif health == "unhealthy":
        note = "it runs, but its own health check says it is unhealthy"
    return Done(
        container=name,
        action=action,
        state=status,
        exit_code=exit_code,
        health=str(health) if health else None,
        note=note,
    )


__all__ = ["SETTLE_SECONDS", "outcome", "settled"]
