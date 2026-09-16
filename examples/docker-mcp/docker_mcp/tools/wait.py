"""Holding until a container is what was asked for.

A reading tool, although it is used between two changing ones: all it does is
read the state until the answer is the one wanted. `docker.wait_for` polls
rather than using the daemon's own `wait`, which knows only about stopping and
cannot report a container becoming healthy.
"""

from __future__ import annotations

import asyncio
from typing import Any

from aiodocker import Docker
from aiodocker.containers import DockerContainer

from docker_mcp import daemon
from docker_mcp.errors import Timeout
from docker_mcp.models import Done, WaitRequest

from .results import outcome


async def wait_for(
    container: DockerContainer, name: str, state: str, seconds: int
) -> dict[str, Any]:
    """Poll until the container reaches `state`, or time runs out.

    The daemon's own `wait` knows only about stopping and cannot report a container becoming
    healthy.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + seconds
    while True:
        current = await daemon.state_of(container)
        status = str(current.get("Status", "unknown"))
        health = str((current.get("Health") or {}).get("Status") or "")
        if state == "healthy" and health == "healthy":
            return current
        if state != "healthy" and status == state:
            return current
        if loop.time() >= deadline:
            said = f"{status}, health {health}" if health else status
            raise Timeout(f"{name} was still {said} after {seconds} seconds")
        await asyncio.sleep(0.25)


async def wait(args: WaitRequest, client: Docker) -> Done:
    """Wait until a container is running, stopped, or reports itself healthy.

    Use this instead of asking for its state in a loop: a container with a
    health check takes seconds to become healthy, and reading too early says
    "starting" and means nothing.
    """
    found = await daemon.find(client, args.container)
    state = await wait_for(found, args.container, args.state, args.seconds)
    return outcome(args.container, f"wait for {args.state}", state, args.state)
