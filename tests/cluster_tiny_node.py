"""One aiohttp-tiny-mcp node of a two-node deployment. Run by tests/test_cluster.py.

    python tests/cluster_tiny_node.py --db /path/state.sqlite --port 8081

Both nodes share one SQLite file, so either can serve any request.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

from aiohttp import web
from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).parent))

from cluster_storage import shared  # noqa: E402

from aiohttp_tiny_mcp import Endpoint, Exchange, NeedInput, Registry, elicit  # noqa: E402
from aiohttp_tiny_mcp.hub import NOTIFICATIONS, topic  # noqa: E402

CONFIG_URI = "config://app"


class Add(BaseModel):
    a: int
    b: int


class Confirm(BaseModel):
    service: str


class Nothing(BaseModel):
    pass


def build(db: str) -> Registry:
    _, hub, sessions = shared(db)
    registry = Registry("cluster", "0.1.0", hub=hub, session_store=sessions, hub_poll_seconds=5.0)

    @registry.tool
    async def add(args: Add) -> int:
        """Add two integers."""
        return args.a + args.b

    @registry.tool
    async def confirm(args: Confirm, ex: Exchange) -> str:
        """Ask once, then act. The round trip may finish on the other node."""
        if (answer := ex.answers.get("confirm")) is not None:
            if answer.get("ok"):
                return f"deployed {ex.state['service']}"
            return "cancelled"
        raise NeedInput(
            {
                "confirm": elicit(
                    f"Deploy {args.service}?",
                    {"type": "object", "properties": {"ok": {"type": "boolean"}}},
                )
            },
            state={"service": args.service},
        )

    @registry.tool
    async def whoami(args: Nothing) -> int:
        """Report the process serving this call, so tests can tell the nodes apart."""
        return os.getpid()

    @registry.tool
    async def touch(args: Nothing) -> str:
        """Announce a resource change for subscribers on any node."""
        await hub.publish(
            topic(NOTIFICATIONS),
            {
                "jsonrpc": "2.0",
                "method": "notifications/resources/updated",
                "params": {"uri": CONFIG_URI},
            },
        )
        return "announced"

    @registry.resource(CONFIG_URI, mime_type="application/json")
    async def config(args: Nothing) -> dict:
        """Application configuration."""
        return {"debug": False}

    return registry


async def serve(db: str, port: int) -> None:
    runner = web.AppRunner(Endpoint(build(db)).app("/mcp"))
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", port).start()
    print(f"READY {port}", flush=True)
    await asyncio.Event().wait()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--port", type=int, required=True)
    options = parser.parse_args()
    asyncio.run(serve(options.db, options.port))


if __name__ == "__main__":
    main()
