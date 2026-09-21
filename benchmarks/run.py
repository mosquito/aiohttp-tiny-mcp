"""Run the benchmarks and print what they measured.

    uv run python -m benchmarks.run                     # everything
    uv run python -m benchmarks.run --suite server
    uv run python -m benchmarks.run --concurrency 32 --calls 3000

Two questions, each answered as a matrix rather than as a pairing.

- **Server**: every server, on every revision, with no client library in the
  way. The bytes of a real request are recorded once and replayed raw.
- **Client**: every client, on every revision, against every server. Nothing
  stops one library's client from driving the other's server, so it does.

Both tables carry the bare web framework each server stands on. Without it a
reader compares aiohttp with uvicorn and believes they compared two MCP
libraries.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from functools import partial
from typing import Any

import aiohttp

from aiohttp_tiny_mcp import Client
from aiohttp_tiny_mcp.client.stdio import StdioClient
from aiohttp_tiny_mcp.protocol.selection import AdapterSet
from benchmarks import servers, wire
from benchmarks.harness import Result, rounds, table

ADAPTERS = {adapter.version: adapter for adapter in AdapterSet.default().adapters}

SDK_REVISION = "2025-11-25"


async def calling_tool(client: Client) -> Any:
    return await client.call_tool("add", {"a": 2, "b": 3})


async def calling_text(client: Client) -> Any:
    return await client.call_tool("text", {})


async def listing_tools(client: Client) -> Any:
    return await client.list_tools()


OPERATIONS: dict[str, Callable[[Client], Awaitable[Any]]] = {
    "call": calling_tool,
    "text": calling_text,
    "list": listing_tools,
}

SDK_CALL: dict[str, dict[str, Any]] = {
    "call": {"tool": "add", "arguments": {"a": 2, "b": 3}},
    "text": {"tool": "text", "arguments": {}},
}


@dataclass
class Servers:
    """Servers started for one benchmark run."""

    ours: str
    loose: str
    keeping: str
    on_aiohttp: str
    on_uvicorn: str


@asynccontextmanager
async def everything() -> AsyncIterator[Servers]:
    async with (
        servers.our_server() as ours,
        sdk_server(stateless=True) as loose,
        sdk_server(stateless=False) as keeping,
        servers.aiohttp_floor() as on_aiohttp,
        starlette_server() as on_uvicorn,
    ):
        yield Servers(ours, loose, keeping, on_aiohttp, on_uvicorn)


@asynccontextmanager
async def sdk_server(*, stateless: bool = True) -> AsyncIterator[str]:
    async with servers.on_uvicorn(servers.sdk_app(stateless=stateless)) as url:
        yield url


@asynccontextmanager
async def starlette_server() -> AsyncIterator[str]:
    async with servers.on_uvicorn(servers.starlette_floor()) as url:
        yield url


def named(implementation: str, version: str, sent: wire.Sent) -> str:
    """The row's name, marked where the server did not accept the revision.

    A server that answers `2026-07-28` by negotiating `2025-11-25` is serving
    the older one. The rate is real; what it is a rate *of* is not what the
    row would otherwise claim.

    `2026-07-28` has no handshake, so nothing is negotiated and nothing can
    be marked. There the shape of the answer is the only evidence, and it is
    the reader's to check.
    """
    if sent.negotiated and sent.negotiated != version:
        return f"{implementation} {version} (spoke {sent.negotiated})"
    return f"{implementation} {version}"


async def bench_server(settings: argparse.Namespace, running: Servers) -> None:
    """Every server, on every revision, driven by nothing but `aiohttp`."""
    ours, loose, keeping = running.ours, running.loose, running.keeping
    async with aiohttp.ClientSession() as session:
        for operation in settings.operations:
            rows: list[tuple[str, Any]] = []
            for implementation, url in (
                ("tiny-mcp", ours),
                ("SDK stateless", loose),
                ("SDK session", keeping),
            ):
                for version, adapter in sorted(ADAPTERS.items()):
                    sent = await wire.record(url, adapter, OPERATIONS[operation])
                    rows.append((named(implementation, version, sent), wire.replay(session, sent)))

            for name, url in (
                ("aiohttp, no protocol", running.on_aiohttp),
                ("uvicorn, no protocol", running.on_uvicorn),
            ):
                fixed = wire.Sent(url, {"jsonrpc": "2.0", "id": 1, "method": "x"}, {})
                rows.append((name, wire.replay(session, fixed)))

            results = await rounds(
                rows,
                calls=settings.calls,
                concurrency=settings.concurrency,
                warmup=settings.warmup,
                repeats=settings.repeats,
            )
            print()
            print(table(f"server: {operation}", results, floor=floors(results)))


def floors(results: list[Result]) -> dict[str, float]:
    """Which bare framework each row is to be read against."""
    reached = {item.name: item.rate for item in results}
    against: dict[str, float] = {}
    for item in results:
        on_starlette = "SDK" in item.name or "uvicorn" in item.name
        base = reached.get("uvicorn, no protocol" if on_starlette else "aiohttp, no protocol")
        if base:
            against[item.name] = base
    return against


async def bench_client(settings: argparse.Namespace, running: Servers) -> None:
    """Every client, on every revision, against every server.

    The cross pairings are the point. A client and a server from one package
    may agree on something neither wrote down, and only the other library's
    implementation finds it.
    """
    ours, loose, keeping = running.ours, running.loose, running.keeping
    async with aiohttp.ClientSession() as session:
        targets = (
            ("tiny-mcp server", ours),
            ("SDK stateless", loose),
            ("SDK session", keeping),
        )
        for operation in settings.operations:
            if operation not in SDK_CALL:
                continue
            rows: list[tuple[str, Any]] = []
            async with AsyncExitStack() as open_clients:
                for target, url in targets:
                    sent = await wire.record(url, ADAPTERS[SDK_REVISION], OPERATIONS[operation])
                    rows.append((f"raw aiohttp -> {target}", wire.replay(session, sent)))

                for target, url in targets:
                    for version, adapter in sorted(ADAPTERS.items()):
                        client = await open_clients.enter_async_context(Client(url, adapter))
                        await client.initialize()
                        rows.append(
                            (
                                f"tiny-mcp {version} -> {target}",
                                partial(OPERATIONS[operation], client),
                            )
                        )

                for target, url in targets:
                    call, spoke = await open_clients.enter_async_context(
                        sdk_client_calling(url, operation)
                    )
                    rows.append((f"official SDK {spoke} -> {target}", call))

                results = await rounds(
                    rows,
                    calls=settings.calls,
                    concurrency=settings.concurrency,
                    warmup=settings.warmup,
                    repeats=settings.repeats,
                )
            print()
            print(table(f"client: {operation}", results))


@asynccontextmanager
async def sdk_client_calling(url: str, operation: str) -> AsyncIterator[tuple[Any, str]]:
    """The official client, held open, with the revision it settled on.

    It appears once per server rather than once per revision. The client
    offers only `LATEST_PROTOCOL_VERSION` and negotiates down from there, and
    nothing in its API asks for a different one -- patching that constant was
    tried and changes nothing, because the server picks. So the revision is
    read back from the handshake instead of assumed.
    """
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    wanted = SDK_CALL[operation]
    async with streamable_http_client(url) as (read, write, *_):
        async with ClientSession(read, write) as session:
            opened = await session.initialize()

            async def call() -> Any:
                return await session.call_tool(wanted["tool"], wanted["arguments"])

            yield call, str(opened.protocol_version)


SCRIPTS = {
    "tiny-mcp": "benchmarks.stdio_ours",
    "SDK": "benchmarks.stdio_sdk",
    "no protocol": "benchmarks.stdio_floor",
}


@asynccontextmanager
async def piped(module: str) -> AsyncIterator[tuple[Any, Any]]:
    """One server as a subprocess, with its pipes."""
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        module,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
    )
    try:
        yield process.stdout, process.stdin
    finally:
        process.terminate()
        await process.wait()


@asynccontextmanager
async def raw_pipe(module: str, operation: str) -> AsyncIterator[Any]:
    """A pipe with no client library on it, for the floor row."""
    wanted = SDK_CALL[operation]
    counter = {"id": 1}
    async with piped(module) as (reader, writer):

        async def call() -> Any:
            counter["id"] += 1
            envelope = {
                "jsonrpc": "2.0",
                "id": counter["id"],
                "method": "tools/call",
                "params": {"name": wanted["tool"], "arguments": wanted["arguments"]},
            }
            writer.write((json.dumps(envelope) + "\n").encode())
            await writer.drain()
            return json.loads(await reader.readline())

        yield call


@asynccontextmanager
async def our_stdio(module: str, adapter: Any, operation: str) -> AsyncIterator[Any]:
    async with StdioClient.spawn(sys.executable, "-m", module, adapter=adapter) as client:
        await client.initialize()
        yield partial(OPERATIONS[operation], client)


@asynccontextmanager
async def sdk_stdio(module: str, operation: str) -> AsyncIterator[tuple[Any, str]]:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    wanted = SDK_CALL[operation]
    parameters = StdioServerParameters(command=sys.executable, args=["-m", module])
    async with stdio_client(parameters) as (read, write):
        async with ClientSession(read, write) as session:
            opened = await session.initialize()

            async def call() -> Any:
                return await session.call_tool(wanted["tool"], wanted["arguments"])

            yield call, str(opened.protocol_version)


async def bench_stdio(settings: argparse.Namespace) -> None:
    """Every client and server pairing over a pipe.

    A subprocess is spawned per row and held for that row's rounds, so the
    process start is paid once and not measured.
    """
    for operation in settings.operations:
        if operation not in SDK_CALL:
            continue
        rows: list[tuple[str, Any]] = []
        async with AsyncExitStack() as spawned:
            rows.append(
                (
                    "raw pipe -> no protocol",
                    await spawned.enter_async_context(raw_pipe(SCRIPTS["no protocol"], operation)),
                )
            )
            for server, module in (("tiny-mcp", SCRIPTS["tiny-mcp"]), ("SDK", SCRIPTS["SDK"])):
                for version, adapter in sorted(ADAPTERS.items()):
                    rows.append(
                        (
                            f"tiny-mcp {version} -> {server}",
                            await spawned.enter_async_context(
                                our_stdio(module, adapter, operation)
                            ),
                        )
                    )
                call, spoke = await spawned.enter_async_context(sdk_stdio(module, operation))
                rows.append((f"official SDK {spoke} -> {server}", call))

            results = await rounds(
                rows,
                calls=settings.calls,
                concurrency=1,
                warmup=settings.warmup,
                repeats=settings.repeats,
            )
        print()
        print(table(f"stdio: {operation}", results))


def parsed() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", choices=("server", "client", "stdio", "all"), default="all")
    parser.add_argument("--calls", type=int, default=1500, help="Measured calls per row.")
    parser.add_argument("--warmup", type=int, default=200, help="Calls before the clock starts.")
    parser.add_argument("--concurrency", type=int, default=1, help="Calls in flight at once.")
    parser.add_argument(
        "--repeats",
        type=int,
        default=3,
        help="Rounds over every row. The best round of each row is reported.",
    )
    parser.add_argument(
        "--operations",
        nargs="*",
        default=["call", "text"],
        choices=sorted(OPERATIONS),
        help="Which operations to time.",
    )
    return parser.parse_args()


async def main() -> None:
    settings = parsed()
    servers.quiet()
    print(
        f"calls={settings.calls} concurrency={settings.concurrency} "
        f"warmup={settings.warmup} repeats={settings.repeats}\n"
        "Every logger is at ERROR.\n"
        "Latencies are milliseconds. `of floor` is the rate as a share of the "
        "same stack answering a fixed body."
    )
    async with everything() as running:
        if settings.suite in ("server", "all"):
            await bench_server(settings, running)
        if settings.suite in ("client", "all"):
            await bench_client(settings, running)
    if settings.suite in ("stdio", "all"):
        await bench_stdio(settings)


if __name__ == "__main__":
    asyncio.run(main())
