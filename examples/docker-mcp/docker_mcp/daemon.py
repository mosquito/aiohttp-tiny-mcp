"""Shared Docker lookups over an application-owned aiodocker client."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from aiodocker import Docker
from aiodocker.containers import DockerContainer
from aiodocker.exceptions import DockerError

from docker_mcp.errors import NotFound, clear
from docker_mcp.models import short


def name_of(raw: Any) -> str:
    names = raw.get("Names") or []
    if names:
        return str(names[0]).lstrip("/")
    return short(str(raw.get("Id", "")))


def when(value: Any) -> str | None:
    """Normalize ISO timestamps and epoch seconds to ISO-8601."""
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()
    text = str(value or "")
    if not text or text.startswith("0001-01-01"):
        return None
    return text


async def containers(client: Docker, *, all: bool = False) -> list[dict[str, Any]]:
    """Extract list response dictionaries from aiodocker container objects."""
    async with clear("listing containers"):
        found = await client.containers.list(all=all)
    return [dict(item._container) for item in found]


async def find(client: Docker, reference: str) -> DockerContainer:
    """Resolve container names or id prefixes, with partial-name fallback. Reject ambiguous
    matches.
    """
    try:
        return await client.containers.get(reference)
    except DockerError:
        pass
    wanted = reference.lstrip("/").lower()
    matches = []
    for raw in await containers(client, all=True):
        identity = str(raw.get("Id", "")).lower()
        names = [str(name).lstrip("/").lower() for name in raw.get("Names") or []]
        if identity.startswith(wanted) or any(wanted in name for name in names):
            matches.append(raw)
    if not matches:
        raise NotFound(f"no container matches {reference!r}")
    if len(matches) > 1:
        named = ", ".join(name_of(raw) for raw in matches[:5])
        raise NotFound(f"{reference!r} matches several containers: {named}")
    return client.containers.container(str(matches[0]["Id"]))


async def state_of(container: DockerContainer) -> dict[str, Any]:
    async with clear("reading the container"):
        raw = await container.show()
    return dict(raw.get("State") or {})


async def images(client: Docker) -> list[dict[str, Any]]:
    async with clear("listing images"):
        return [dict(item) for item in await client.images.list()]
