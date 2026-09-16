"""What the daemon did lately, rather than what is true now.

No amount of reading the current state says when a container last restarted.
The daemon keeps that, and `docker.history` asks for it with an `until`, which
is what makes the read finish instead of following what happens next.
"""

from __future__ import annotations

import time
from typing import Any

from aiodocker import Docker
from aiodocker.jsonstream import json_stream_list

from docker_mcp import daemon
from docker_mcp.errors import clear
from docker_mcp.models import Event, EventFilter, Events, short


def event_of(raw: dict[str, Any]) -> Event:
    actor = raw.get("Actor") or {}
    attributes = actor.get("Attributes") or {}
    exit_code = attributes.get("exitCode")
    at = raw.get("time")
    return Event(
        at=daemon.when(at) or "",
        kind=str(raw.get("Type", "")),
        action=str(raw.get("Action", "")),
        subject=str(attributes.get("name") or short(str(actor.get("ID", "")))),
        exit_code=int(exit_code) if exit_code not in (None, "") else None,
    )


def epoch() -> float:
    """Now, in the seconds the daemon's event filters count in."""
    return time.time()


async def history(client: Docker, since: float, until: float) -> list[dict[str, Any]]:
    """Read recorded events. `until` bounds the read; without it the daemon holds the connection
    open for events still to come.
    """
    async with clear("reading the daemon's events"):
        async with client._query(
            "events", method="GET", params={"since": int(since), "until": int(until)}
        ) as response:
            raw = await json_stream_list(response, raise_on_error=False)
    return [dict(item) for item in raw]


async def events(args: EventFilter, client: Docker) -> Events:
    """What the daemon has done lately: containers started, died or removed.

    This is how to answer "when did it restart" and "what happened just
    before it stopped", which no amount of reading the current state will
    say. Ask for a wider `seconds` if nothing comes back.
    """
    now = epoch()
    listed = [event_of(raw) for raw in await history(client, now - args.seconds, now)]
    if args.container:
        wanted = args.container.lower()
        listed = [item for item in listed if wanted in item.subject.lower()]
    kept = listed[-args.limit :]
    return Events(events=kept, shown=len(kept), total=len(listed))
