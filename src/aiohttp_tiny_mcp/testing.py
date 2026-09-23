"""Test helpers for in-memory and HTTP MCP clients."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from typing import Any

from aiohttp import web

from .client.base import BaseClient, Elicitor
from .client.http import Client
from .client.stdio import DEFAULT_STREAM_LIMIT, StdioClient
from .protocol.adapter import Adapter
from .protocol.core import elicit_accept, elicit_decline
from .protocol.models import Implementation
from .protocol.selection import AdapterSet
from .server.http import Endpoint
from .server.registry import Registry
from .server.stdio import serve_stdio


class Pipe:
    """One direction of an in-memory connection."""

    def __init__(self, *, limit: int = DEFAULT_STREAM_LIMIT) -> None:
        self.reader = asyncio.StreamReader(limit=limit)

    def write(self, data: bytes) -> None:
        self.reader.feed_data(data)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.reader.feed_eof()


def answering(answers: Mapping[str, Any] | None) -> Elicitor | None:
    """Build an elicitation callback from a mapping."""
    if answers is None:
        return None

    async def answer(request: Mapping[str, Any]) -> Mapping[str, Any]:
        key = str((request.get("params") or {}).get("message", ""))
        for wanted, value in answers.items():
            if wanted in key:
                return elicit_decline() if value is None else elicit_accept(value)
        return elicit_decline()

    return answer


@asynccontextmanager
async def connect(
    registry: Registry,
    *,
    adapter: Adapter | str | None = None,
    answers: Mapping[str, Any] | None = None,
    on_ask: Elicitor | None = None,
    client_info: Implementation | None = None,
    initialize: bool = True,
    limit: int = DEFAULT_STREAM_LIMIT,
) -> AsyncIterator[BaseClient]:
    """A client talking to `registry` through memory.

    No port, no subprocess, and nothing to tear down. The protocol is the
    real one: pick `adapter` to see what a client of that revision sees.

    `limit` bounds request and response lines in bytes. The default is
    1 MiB, matching `StdioClient.spawn`.

    Answer a handler's questions with `answers`, keyed by any part of the
    question's text::

        async with connect(registry, answers={"Deploy": {"ok": True}}) as client:
            result = await client.call_tool("deploy", {"service": "web"})
    """
    chosen = pick(adapter)
    to_server, to_client = Pipe(limit=limit), Pipe(limit=limit)

    def send(payload: Mapping[str, Any]) -> None:
        to_client.write((json.dumps(payload, ensure_ascii=False) + "\n").encode())

    served = asyncio.ensure_future(serve_stdio(registry, to_server.reader, send))
    client = StdioClient(
        to_client.reader,
        to_server,
        chosen,
        client_info=client_info,
        on_ask=on_ask or answering(answers),
    )
    try:
        if initialize:
            await client.initialize()
        yield client
    finally:
        to_server.close()
        served.cancel()
        await asyncio.gather(served, return_exceptions=True)


@asynccontextmanager
async def serving(
    registry: Registry,
    *,
    path: str = "/mcp",
    adapters: AdapterSet | None = None,
    middlewares: list[Any] | None = None,
) -> AsyncIterator[str]:
    """`registry` on a loopback port, for the examples that are about HTTP.

    Yields the URL. The port is whichever one was free, so nothing collides
    with anything else running.
    """
    app = web.Application(middlewares=middlewares or [])
    Endpoint(registry, adapters=adapters).setup(app, path)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    host, port = runner.addresses[0]
    try:
        yield f"http://{host}:{port}{path}"
    finally:
        await runner.cleanup()


@asynccontextmanager
async def over_http(
    registry: Registry,
    *,
    adapter: Adapter | str | None = None,
    answers: Mapping[str, Any] | None = None,
    on_ask: Elicitor | None = None,
    headers: Mapping[str, str] | None = None,
    initialize: bool = True,
) -> AsyncIterator[Client]:
    """A real HTTP client against a real server, both started here.

    Use it where the transport is the subject: a session that has to survive
    a second request, a stream, a header the server reads. Where it is not,
    `connect` says the same thing without a socket.
    """
    import aiohttp

    async with serving(registry) as url:
        async with aiohttp.ClientSession(headers=dict(headers or {})) as session:
            async with Client(
                url,
                pick(adapter),
                session=session,
                on_ask=on_ask or answering(answers),
            ) as client:
                if initialize:
                    await client.initialize()
                yield client


def pick(adapter: Adapter | str | None) -> Adapter:
    """The adapter to speak, named or taken whole.

    Defaults to the newest revision, which is what a client that knows
    nothing about this server would offer.
    """
    known = AdapterSet.default()
    if adapter is None:
        return known.adapters[0]
    if isinstance(adapter, str):
        return known.by_version[adapter]
    return adapter


async def every_revision(
    registry: Registry,
    call: Callable[[BaseClient], Awaitable[Any]],
    *,
    answers: Mapping[str, Any] | None = None,
    on_ask: Elicitor | None = None,
) -> dict[str, Any]:
    """Run `call` once per revision, and report what each one saw.

    The claim this package makes is that one declaration serves five
    revisions. This is how an application checks that claim about its own
    handlers, in one line instead of a loop with a fixture in it.
    """
    seen: dict[str, Any] = {}
    for adapter in AdapterSet.default().adapters:
        async with connect(registry, adapter=adapter, answers=answers, on_ask=on_ask) as client:
            seen[adapter.version] = await call(client)
    return seen


__all__ = [
    "Pipe",
    "answering",
    "connect",
    "every_revision",
    "over_http",
    "pick",
    "serving",
]
