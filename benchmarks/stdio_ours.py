"""This package's server over stdin and stdout, for the stdio benchmark."""

from __future__ import annotations

import asyncio

from aiohttp_tiny_mcp import run_stdio
from benchmarks.servers import add, our_registry, text  # noqa: F401

if __name__ == "__main__":
    asyncio.run(run_stdio(our_registry()))
