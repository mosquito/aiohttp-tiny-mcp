"""One container, from the daemon's `inspect` shape.

The reshaping lives in `docker.py`. What this file owes a reader is the
promise it keeps: environment values never leave the daemon, only their names,
because a value is a password as often as not.
"""

from __future__ import annotations

import shlex
from typing import Any

from aiodocker import Docker

from docker_mcp import daemon
from docker_mcp.errors import clear
from docker_mcp.models import ContainerDetail, ContainerRef, short

PROJECT_LABEL = "com.docker.compose.project"
SERVICE_LABEL = "com.docker.compose.service"


def published_ports(raw: dict[str, Any]) -> list[str]:
    bindings = (raw.get("NetworkSettings") or {}).get("Ports") or {}
    published: list[str] = []
    for private, hosts in bindings.items():
        for binding in hosts or []:
            published.append(
                f"{binding.get('HostIp', '')}:{binding.get('HostPort', '')}->{private}"
            )
        if not hosts:
            published.append(str(private))
    return published


def command_of(config: dict[str, Any], raw: dict[str, Any]) -> str:
    """What the container runs, quoted as a shell would quote it.

    Joining the arguments with spaces loses the difference between one
    argument containing a space and two arguments, which is the difference
    between what ran and something else.
    """
    parts = [*(config.get("Entrypoint") or []), *(config.get("Cmd") or [])]
    if parts:
        return shlex.join(str(part) for part in parts)
    return str(raw.get("Path", ""))


def compose_of(config: dict[str, Any]) -> str | None:
    """Which Compose service this is, where it is one."""
    labels = config.get("Labels") or {}
    project = labels.get(PROJECT_LABEL)
    service = labels.get(SERVICE_LABEL)
    if project and service:
        return f"{project}/{service}"
    return str(project) if project else None


def detail(raw: dict[str, Any]) -> ContainerDetail:
    """One container, from the daemon's `inspect` shape rather than its list
    shape -- the two differ, which is why this is not `summary` with extras."""
    state = raw.get("State") or {}
    config = raw.get("Config") or {}
    host = raw.get("HostConfig") or {}
    networks = (raw.get("NetworkSettings") or {}).get("Networks") or {}
    health = (state.get("Health") or {}).get("Status")
    exit_code = state.get("ExitCode")
    return ContainerDetail(
        id=short(str(raw.get("Id", ""))),
        name=str(raw.get("Name", "")).lstrip("/"),
        image=str(config.get("Image", "")),
        state=str(state.get("Status", "unknown")),
        ports=published_ports(raw),
        created=daemon.when(raw.get("Created")) or "",
        started=daemon.when(state.get("StartedAt")),
        finished=daemon.when(state.get("FinishedAt")),
        command=command_of(config, raw),
        restart_count=int(raw.get("RestartCount", 0) or 0),
        restart_policy=str((host.get("RestartPolicy") or {}).get("Name") or "no"),
        health=str(health) if health else None,
        exit_code=int(exit_code)
        if exit_code is not None and state.get("Status") != "running"
        else None,
        error=str(state.get("Error")) or None,
        compose=compose_of(config),
        networks=sorted(networks),
        mounts=[
            f"{mount.get('Source', '')}:{mount.get('Destination', '')}"
            for mount in raw.get("Mounts") or []
        ],
        environment=sorted(str(item).split("=", 1)[0] for item in config.get("Env") or []),
    )


async def container(args: ContainerRef, client: Docker) -> ContainerDetail:
    """Everything about one container: health, exit code, restarts, mounts,
    networks, its Compose service, and the names of its environment variables.

    Values of environment variables are never reported. Use this when a
    container is behaving oddly and the list did not say why.
    """
    found = await daemon.find(client, args.container)
    async with clear("reading", subject=args.container):
        return detail(await found.show())
