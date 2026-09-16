"""Fixtures for Markdown examples executed by markdown-pytest against a real HTTP server."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import AsyncExitStack

import pytest

from aiohttp_tiny_mcp import Registry
from aiohttp_tiny_mcp.protocol.selection import AdapterSet
from aiohttp_tiny_mcp.testing import serving


@pytest.fixture
async def serve():
    """Put an `Endpoint` on a real socket and answer with its URL.

    Nothing here that an example could not write itself: this is
    `aiohttp_tiny_mcp.testing.serving`, held open until the example ends.
    """
    async with AsyncExitStack() as running:

        async def start(registry: Registry, path: str = "/mcp") -> str:
            return await running.enter_async_context(serving(registry, path=path))

        yield start


@pytest.fixture
def adapters() -> AdapterSet:
    return AdapterSet.default()


@pytest.fixture
def every_revision(adapters: AdapterSet) -> list:
    """Every revision this package speaks, newest first."""
    return list(adapters.adapters)


@pytest.fixture
async def printed() -> AsyncIterator[list]:
    """Somewhere for an example to put what it would otherwise print."""
    yield []


@pytest.fixture(name="__name__")
def module_name() -> str:
    """A name for the block being executed.

    A page's examples are written as whole modules, ending with the
    `if __name__ == "__main__":` that runs them. That line has to mean
    something here, and it has to be false: the point of running the examples
    is to check them, not to start a server the suite would then wait on.
    """
    return "docs.example"


@pytest.fixture
def stdio_server(tmp_path) -> str:
    """A server over standard streams, written the way the pages say to write
    one, so the client example on this page has something real to spawn."""
    script = tmp_path / "server.py"
    script.write_text(
        '''import asyncio

from pydantic import BaseModel

from aiohttp_tiny_mcp import MemoryHub, MemorySessionStore, Registry, run_stdio

registry = Registry("adder", "0.1.0", hub=MemoryHub(), session_store=MemorySessionStore())


class Add(BaseModel):
    a: int
    b: int


@registry.tool
async def add(args: Add) -> int:
    """Add two integers."""
    return args.a + args.b


if __name__ == "__main__":
    asyncio.run(run_stdio(registry))
''',
        encoding="utf-8",
    )
    return str(script)
