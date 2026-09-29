"""What one request costs inside the server, with no HTTP under it.

    uv run python -m benchmarks.schema_cost
    uv run python -m benchmarks.schema_cost --loops 5000 --profile

The matrix in `run.py` answers a request through a socket, so a change inside
the protocol arrives there divided by the cost of the transport. This module
removes the transport: it decodes recorded bytes, dispatches the call, encodes
the result and serializes it, in one process.

Two tool shapes are timed, because the cost of a revision depends on the
schema it has to project. `small` is the tool the matrix uses. `fat` is what
a real catalogue holds: nested models, enums and nullable unions, so `$defs`
and `anyOf` both appear.

Revisions before 2026-07-28 simplify a schema for the client. That work is
cached per revision on the tool, so it belongs to registration rather than to
the request. This module is what shows the cache is still there.
"""

from __future__ import annotations

import argparse
import asyncio
import cProfile
import json
import pstats
import time
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field

from aiohttp_tiny_mcp import MemoryHub, MemorySessionStore, Registry
from aiohttp_tiny_mcp.protocol.adapter import Adapter
from aiohttp_tiny_mcp.protocol.core import Call, Operation, Preamble
from aiohttp_tiny_mcp.protocol.models import serialized
from aiohttp_tiny_mcp.protocol.selection import AdapterSet
from aiohttp_tiny_mcp.server.dispatcher import Dispatcher
from aiohttp_tiny_mcp.server.exchange import Exchange

ADAPTERS = {adapter.version: adapter for adapter in AdapterSet.default().adapters}


class Add(BaseModel):
    a: int
    b: int


class Sum(BaseModel):
    result: int


async def add(args: Add) -> Sum:
    """Add two integers."""
    return Sum(result=args.a + args.b)


class Status(str, Enum):
    todo = "todo"
    doing = "doing"
    done = "done"


class Link(BaseModel):
    kind: Literal["depends_on", "parent"] = "depends_on"
    target: int
    note: str | None = None


class Person(BaseModel):
    name: str
    email: str | None = None
    tags: list[str] = Field(default_factory=list)


class Record(BaseModel):
    """One task, shaped like a tracker's own."""

    title: str
    body: str = ""
    status: Status = Status.todo
    priority: Literal["urgent", "high", "normal", "low"] = "normal"
    assignee: Person | None = None
    reporter: Person | None = None
    watchers: list[Person] = Field(default_factory=list)
    links: list[Link] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    estimate: float | None = None
    parent: int | None = None
    project: str | None = None


class Stored(BaseModel):
    id: int
    title: str
    status: Status
    assignee: Person | None = None
    links: list[Link] = Field(default_factory=list)


async def record(args: Record) -> Stored:
    """Create one task."""
    return Stored(id=1, title=args.title, status=args.status)


SHAPES = {"small": (add, {"a": 2, "b": 3}), "fat": (record, {"title": "x"})}


class Detached:
    """Request app access, for a dispatcher running outside a web server."""

    app: dict[Any, Any] = {}


def served(tool: Any) -> Dispatcher:
    registry = Registry("schema-cost", "0.1.0", hub=MemoryHub(), session_store=MemorySessionStore())
    registry.tool(tool)
    return Dispatcher(registry)


def envelope(adapter: Adapter, operation: Operation, name: str, arguments: dict) -> bytes:
    """The bytes a client would send for `operation`, as this revision wants them."""
    params: dict[str, Any] = {}
    if operation is Operation.CALL_TOOL:
        params = {"name": name, "arguments": arguments}
    if adapter.version == "2026-07-28":
        # This revision has no handshake, so every request carries its context.
        params["_meta"] = {
            "io.modelcontextprotocol/protocolVersion": adapter.version,
            "io.modelcontextprotocol/clientInfo": {"name": "schema-cost", "version": "0"},
            "io.modelcontextprotocol/clientCapabilities": {},
        }
    method = adapter.method_for(operation)
    return json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()


async def answered(dispatcher: Dispatcher, adapter: Adapter, raw: bytes) -> bytes:
    """One request, from bytes in to bytes out."""
    (decoded,) = adapter.decode(Preamble.of(raw, {}), dispatcher.registry)
    assert isinstance(decoded, Call), decoded
    call = decoded
    exchange = Exchange(dispatcher.registry, Detached(), adapter, call, None)
    outcome = await dispatcher.run(exchange)
    return serialized(adapter.encode(call, dispatcher.registry, outcome))


async def timed(label: str, dispatcher: Dispatcher, adapter: Adapter, raw: bytes, loops: int):
    answer = await answered(dispatcher, adapter, raw)  # warm up, and size the answer
    started = time.perf_counter()
    for _ in range(loops):
        await answered(dispatcher, adapter, raw)
    elapsed = time.perf_counter() - started
    print(
        f"{label:<34} {loops / elapsed:>9.0f} ops/s "
        f"{elapsed / loops * 1e6:>8.1f} us {len(answer):>6} B"
    )


async def profiled(loops: int) -> None:
    """Where the time goes for the most expensive pairing."""
    dispatcher = served(record)
    adapter = ADAPTERS["2025-11-25"]
    raw = envelope(adapter, Operation.LIST_TOOLS, "record", {})
    await answered(dispatcher, adapter, raw)
    profiler = cProfile.Profile()
    profiler.enable()
    for _ in range(loops):
        await answered(dispatcher, adapter, raw)
    profiler.disable()
    print("\nprofile: 2025-11-25 tools/list, fat schema")
    pstats.Stats(profiler).sort_stats("tottime").print_stats(12)


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--loops", type=int, default=3000, help="Measured requests per row.")
    parser.add_argument("--profile", action="store_true", help="Also print a cProfile table.")
    settings = parser.parse_args()

    print("No HTTP. Answer sizes are the bytes the server would write.")
    for shape, (tool, arguments) in SHAPES.items():
        dispatcher = served(tool)
        name = tool.__name__
        print(f"\n{shape} schema")
        for version in sorted(ADAPTERS):
            adapter = ADAPTERS[version]
            for operation in (Operation.CALL_TOOL, Operation.LIST_TOOLS):
                raw = envelope(adapter, operation, name, arguments)
                label = f"{version} {adapter.method_for(operation)}"
                await timed(label, dispatcher, adapter, raw, settings.loops)

    if settings.profile:
        await profiled(settings.loops)


if __name__ == "__main__":
    asyncio.run(main())
