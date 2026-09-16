"""Record and replay requests for benchmarks."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

import aiohttp

from aiohttp_tiny_mcp import Client
from aiohttp_tiny_mcp.adapter import Adapter


@dataclass
class Sent:
    """One request, ready to be posted again."""

    url: str
    envelope: dict[str, Any]
    headers: dict[str, str]
    negotiated: str = ""


class Watching:
    """An `aiohttp.ClientSession` that remembers the last POST it carried."""

    def __init__(self, session: aiohttp.ClientSession) -> None:
        self.session = session
        self.last: tuple[dict[str, Any], dict[str, str]] | None = None

    def post(self, url: str, *, json: Any = None, headers: Any = None, **rest: Any):
        if isinstance(json, dict) and json.get("method"):
            self.last = (json, dict(headers or {}))
        return self.session.post(url, json=json, headers=headers, **rest)

    def get(self, *args: Any, **kw: Any):
        return self.session.get(*args, **kw)


async def record(url: str, adapter: Adapter, operation: Callable[[Client], Awaitable[Any]]) -> Sent:
    """Perform `operation` once for real, and keep what went out.

    The handshake runs first where the revision has one, so the recorded
    headers carry a session the server has actually issued.
    """
    async with aiohttp.ClientSession() as session:
        watching = Watching(session)
        client = Client(url, adapter, session=watching)  # type: ignore[arg-type]
        opened = await client.initialize()
        await operation(client)
        assert watching.last is not None, "nothing was posted"
        envelope, headers = watching.last
        spoke = str(opened.get("protocolVersion") or "")
        return Sent(url, dict(envelope), headers, spoke)


async def reply(response: aiohttp.ClientResponse) -> Mapping[str, Any]:
    """The one frame that answers, whichever way the server framed it."""
    if response.content_type != "text/event-stream":
        return await response.json(content_type=None)
    async for raw in response.content:
        line = raw.decode("utf-8", "replace").strip()
        if line.startswith("data:"):
            return json.loads(line[len("data:") :].strip())
    raise RuntimeError("the stream ended without an answer")


def replay(session: aiohttp.ClientSession, sent: Sent) -> Callable[[], Awaitable[Any]]:
    """A callable that posts the recorded request again.

    The id changes on every call, because two requests sharing one id is a
    thing no real client does and not a thing worth measuring.
    """
    counter = {"id": 1_000_000}

    async def call() -> Any:
        counter["id"] += 1
        envelope = {**sent.envelope, "id": counter["id"]}
        async with session.post(sent.url, json=envelope, headers=sent.headers) as response:
            answered = await reply(response)
        if "error" in answered:
            raise RuntimeError(str(answered["error"]))
        return answered

    return call
