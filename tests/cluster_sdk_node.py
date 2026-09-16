"""One official-SDK node of a two-node deployment. Run by tests/test_cluster.py.

    python tests/cluster_sdk_node.py --db /path/state.sqlite --port 8083 --key <hex>

The node plugs the SDK's three sharing seams into the same SQLite file the
aiohttp-tiny-mcp nodes use:

- `request_state_security=` seals the round-trip state under a shared key.
- `subscriptions=` fans `subscriptions/listen` events out across processes.
- `event_store=` records the stateful transport's events.

`--ephemeral-state` mints a process-local key instead, to show what a node that
shares nothing does with another node's state.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path
from typing import Annotated

import uvicorn
from mcp.server.mcpserver import Elicit, MCPServer, RequestStateSecurity, Resolve
from mcp.shared.subscriptions import ResourceUpdated
from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).parent))

from cluster_storage import SqlEventStore, SqlSubscriptionBus, shared  # noqa: E402

CONFIG_URI = "config://app"


class Ok(BaseModel):
    ok: bool


def ask_confirm(service: str) -> Elicit[Ok]:
    """Resolver for `confirm`. It names the question the framework asks."""
    return Elicit(f"Deploy {service}?", Ok)


def build(db: str, key: str | None):
    _, hub, _sessions = shared(db)
    bus = SqlSubscriptionBus(hub)
    security = RequestStateSecurity(keys=[key]) if key else RequestStateSecurity.ephemeral()
    server = MCPServer("cluster", request_state_security=security, subscriptions=bus)

    @server.tool()
    def add(a: int, b: int) -> int:
        """Add two integers."""
        return a + b

    @server.tool()
    def confirm(service: str, answer: Annotated[Ok, Resolve(ask_confirm)]) -> str:
        """Ask once, then act. The round trip may finish on the other node."""
        return f"deployed {service}" if answer.ok else "cancelled"

    @server.tool()
    def whoami() -> int:
        """Report the process serving this call, so tests can tell the nodes apart."""
        return os.getpid()

    @server.tool()
    async def touch() -> str:
        """Announce a resource change for subscribers on any node."""
        await bus.publish(ResourceUpdated(uri=CONFIG_URI))
        return "announced"

    @server.resource(CONFIG_URI)
    def config() -> dict:
        """Application configuration."""
        return {"debug": False}

    return server.streamable_http_app(stateless_http=False, event_store=SqlEventStore(hub))


async def serve(db: str, port: int, key: str | None) -> None:
    app = build(db, key)
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    task = asyncio.ensure_future(server.serve())
    while not server.started:
        await asyncio.sleep(0.01)
    print(f"READY {port}", flush=True)
    await task


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--key", default=None)
    parser.add_argument("--ephemeral-state", action="store_true")
    options = parser.parse_args()
    key = None if options.ephemeral_state else options.key
    asyncio.run(serve(options.db, options.port, key))


if __name__ == "__main__":
    main()
