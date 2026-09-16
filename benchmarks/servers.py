"""Servers used by the benchmark suite."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from aiohttp import web
from pydantic import BaseModel

from aiohttp_tiny_mcp import Endpoint, MemoryHub, MemorySessionStore, Registry
from aiohttp_tiny_mcp.protocol.selection import AdapterSet


class Add(BaseModel):
    a: int
    b: int


class Sum(BaseModel):
    result: int


class Nothing(BaseModel):
    """No arguments."""


GREETING = "hello"


async def add(args: Add) -> Sum:
    """Add two integers."""
    return Sum(result=args.a + args.b)


async def text(args: Nothing) -> str:
    """Return a string."""
    return GREETING


def our_registry() -> Registry:
    registry = Registry(
        "bench",
        "0.1.0",
        hub=MemoryHub(),
        session_store=MemorySessionStore(),
        instructions="A server that adds two integers.",
    )
    registry.tool(add)
    registry.tool(text)
    registry.resource("bench://config", config)
    return registry


async def config(args: BaseModel) -> dict:
    """A fixed resource, for the read benchmark."""
    return {"debug": False}


@asynccontextmanager
async def our_server() -> AsyncIterator[str]:
    """This package, on aiohttp."""
    registry = our_registry()
    app = Endpoint(registry, adapters=AdapterSet.default()).app("/mcp")
    async with listening(app) as url:
        yield url


@asynccontextmanager
async def aiohttp_floor() -> AsyncIterator[str]:
    """Serve a fixed JSON-RPC reply with aiohttp."""

    async def handle(request: web.Request) -> web.Response:
        body = await request.json()
        return web.json_response(
            {
                "jsonrpc": "2.0",
                "id": body.get("id"),
                "result": {"structuredContent": {"result": 5}, "content": []},
            }
        )

    app = web.Application()
    app.router.add_post("/mcp", handle)
    async with listening(app) as url:
        yield url


@asynccontextmanager
async def listening(app: web.Application) -> AsyncIterator[str]:
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    host, port = runner.addresses[0]
    try:
        yield f"http://{host}:{port}/mcp"
    finally:
        await runner.cleanup()


def sdk_app(*, stateless: bool):
    """Build the official SDK server."""
    from mcp.server.mcpserver import MCPServer

    server = MCPServer("bench")

    @server.tool()
    def add(a: int, b: int) -> Sum:  # noqa: F811 -- the SDK wants its own signature
        return Sum(result=a + b)

    @server.tool()
    def text() -> str:  # noqa: F811
        return GREETING

    @server.resource("bench://config")
    def config() -> dict:  # noqa: F811
        return {"debug": False}

    return server.streamable_http_app(stateless_http=stateless)


def starlette_floor():
    """The bare framework the SDK stands on, answering the same fixed reply."""
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    async def handle(request: Request) -> JSONResponse:
        body = await request.json()
        return JSONResponse(
            {
                "jsonrpc": "2.0",
                "id": body.get("id"),
                "result": {"structuredContent": {"result": 5}, "content": []},
            }
        )

    return Starlette(routes=[Route("/mcp", handle, methods=["POST"])])


NOISY = ("mcp", "sse_starlette", "uvicorn", "httpx", "httpcore")


def quiet() -> None:
    """Disable request logging during a benchmark."""
    logging.getLogger().setLevel(logging.ERROR)
    for name in NOISY:
        logging.getLogger(name).setLevel(logging.ERROR)
        logging.getLogger(name).propagate = False


@asynccontextmanager
async def on_uvicorn(app) -> AsyncIterator[str]:
    import uvicorn

    quiet()
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error", access_log=False)
    sock = config.bind_socket()
    port = sock.getsockname()[1]
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        while not server.started:
            await asyncio.sleep(0.01)
        quiet()
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        server.should_exit = True
        await task
