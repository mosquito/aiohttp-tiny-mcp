"""Runnable demo server exercising every registration kind.

    uv run python examples/demo.py
    uv run python examples/demo.py --stdio

Then, for a 2026-07-28 client:
    curl localhost:8080/mcp -H 'Content-Type: application/json' \\
        -H 'Accept: application/json, text/event-stream' \\
        -H 'MCP-Protocol-Version: 2026-07-28' \\
        -H 'Mcp-Method: server/discover' \\
        -d '{"jsonrpc":"2.0","id":1,"method":"server/discover","params":{"_meta":{
             "io.modelcontextprotocol/protocolVersion":"2026-07-28",
             "io.modelcontextprotocol/clientCapabilities":{}}}}'

Or a legacy client:
    curl localhost:8080/mcp -H 'Content-Type: application/json' \\
        -H 'Accept: application/json, text/event-stream' \\
        -d '{"jsonrpc":"2.0","id":1,"method":"initialize",
             "params":{"protocolVersion":"2025-11-25"}}'
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from collections.abc import AsyncIterator

from aiohttp import web
from pydantic import BaseModel

from aiohttp_tiny_mcp import (
    Endpoint,
    Exchange,
    MemoryHub,
    MemorySessionStore,
    Registry,
    elicit,
    namespace,
    run_stdio,
)
from aiohttp_tiny_mcp.console import Console
from aiohttp_tiny_mcp.protocol.models import CompleteParams
from aiohttp_tiny_mcp.storage.hub import NOTIFICATIONS, topic


class Pool:
    async def query(self, sql: str) -> str:
        return f"rows for {sql!r}"


POOL = web.AppKey("pool", Pool)


async def pool_ctx(app: web.Application) -> AsyncIterator[None]:
    app[POOL] = Pool()
    yield


class User:
    def __init__(self, name: str) -> None:
        self.name = name


USER: web.RequestKey[str] = web.RequestKey("user", str)


async def current_user(ex: Exchange) -> User:
    name = ex.request.get(USER, "anon") if isinstance(ex.request, web.Request) else "anon"
    return User(name)


hub = MemoryHub()
registry = Registry(
    "demo",
    "0.1.0",
    hub=hub,
    instructions="Toy server. Speaks every revision this package supports.",
    session_store=MemorySessionStore(),
)
registry.provide(Pool, POOL)
registry.provide(User, current_user)


class Add(BaseModel):
    a: int
    b: int


class Sum(BaseModel):
    result: int


@registry.tool
async def add(args: Add) -> Sum:
    """Add two integers."""
    return Sum(result=args.a + args.b)


class Nothing(BaseModel):
    pass


SETTINGS: dict[str, bool] = {"debug": False}


@registry.resource("config://app", mime_type="application/json")
async def config(args: Nothing) -> dict:
    """Application configuration."""
    return dict(SETTINGS)


class Debug(BaseModel):
    debug: bool


@registry.tool
async def set_debug(args: Debug) -> str:
    """Change the configuration and notify subscribers."""
    SETTINGS["debug"] = args.debug
    await hub.publish(
        topic(NOTIFICATIONS),
        {
            "jsonrpc": "2.0",
            "method": "notifications/resources/updated",
            "params": {"uri": "config://app"},
        },
    )
    return f"debug is now {args.debug}"


class UserRef(BaseModel):
    id: int


@registry.resource("db://users/{id}", name="user", mime_type="application/json")
async def user_row(args: UserRef, pool: Pool) -> str:
    """One user by id. Template variables arrive as the args model."""
    return await pool.query(f"select * from users where id={args.id}")


class Review(BaseModel):
    language: str


@registry.prompt
async def review(args: Review) -> str:
    """Ask for a code review."""
    return f"Please review this {args.language} code."


@registry.completions
async def completions(args: CompleteParams) -> list[str]:
    if args.ref.type == "ref/prompt" and args.argument.name == "language":
        return [x for x in ("python", "rust", "go") if x.startswith(args.argument.value)]
    return []


NO_SESSION = (
    "no session reached. On 2026-07-28 call session_open first and pass its "
    "handle; on a legacy revision the Mcp-Session-Id header carries one."
)


class Deploy(BaseModel):
    service: str


@registry.tool
async def deploy(args: Deploy, ex: Exchange) -> str:
    """Ask for confirmation before deploying."""
    confirm = await ex.ask("confirm", elicit(f"Deploy {args.service} to production?"))
    if not confirm.accepted:
        return f"did not deploy {args.service}: client sent {confirm.action}"
    return f"deployed {args.service}"


class SessionValue(BaseModel):
    key: str
    value: str
    handle: str | None = None


class SessionRef(BaseModel):
    handle: str | None = None


async def reach_session(ex: Exchange, handle: str | None):
    """The session this call means: the handle if given, else the request's own."""
    if handle is None:
        return ex.session
    return await ex.sessions.use(handle) if ex.sessions is not None else None


@registry.tool
async def session_open(args: Nothing, ex: Exchange) -> dict:
    """Create a session handle. Pass it to session_set/session_info on later calls, including on
    revisions without protocol sessions.
    """
    if ex.sessions is None:
        return {"handle": None, "reason": "this server has no session store"}
    session = await ex.sessions.open()
    return {"handle": session.id}


@registry.tool
async def session_set(args: SessionValue, ex: Exchange) -> str:
    """Store a value that outlives this request."""
    session = await reach_session(ex, args.handle)
    if session is None:
        return NO_SESSION
    await session.set(args.key, args.value)
    return f"session {session.id}: {args.key} = {args.value!r}"


@registry.tool
async def session_info(args: SessionRef, ex: Exchange) -> dict:
    """Show which session this call reaches, and what it holds."""
    session = await reach_session(ex, args.handle)
    if session is None:
        return {"session": None, "reason": NO_SESSION}
    return {"session": session.id, "values": dict(session.values)}


@registry.tool(streaming=True)
async def slow_count(args: Add, ex: Exchange) -> str:
    """Report progress and messages before returning the count."""
    await ex.log("debug", f"counting from {args.a} to {args.b}", logger="slow_count")
    for i in range(args.a, args.b):
        await ex.progress(i - args.a, args.b - args.a)
        await asyncio.sleep(0.05)
    await ex.log("info", "done", logger="slow_count")
    return f"counted {args.b - args.a}"


@web.middleware
async def auth(request: web.Request, handler):
    request[USER] = request.headers.get("Authorization", "anon")
    return await handler(request)


@web.middleware
async def tenant(request: web.Request, handler):
    """Isolate demo callers by forwarded address or request.remote. Production deployments should
    use an authenticated account or token subject.
    """
    namespace.set(request.headers.get("X-Tenant") or request.remote)
    return await handler(request)


async def health(request: web.Request) -> web.Response:
    return web.Response(text="ok")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stdio", action="store_true", help="serve over stdio instead of HTTP")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    if args.stdio:
        # stdout is reserved for the stdio protocol.
        logging.basicConfig(level=logging.INFO, stream=sys.stderr)
        asyncio.run(run_stdio(registry, app_state={POOL: Pool()}))
    else:
        logging.basicConfig(level=logging.INFO)
        # Enable protocol DEBUG logging without increasing aiohttp/asyncio verbosity.
        logging.getLogger("aiohttp_tiny_mcp").setLevel(logging.DEBUG)
        app = web.Application(middlewares=[auth, tenant])
        app.cleanup_ctx.append(pool_ctx)
        app.add_routes([web.get("/health", health)])
        Endpoint(
            registry,
            # Behind a reverse proxy, uncomment this and let the proxy check Origin.
            # trust_proxy_origin_validation=True,
        ).setup(app, "/mcp")
        Console(
            "/mcp",
            title="Demo MCP server",
            description=(
                "Every feature this package has, on one toy server: tools that ask, "
                "resources that change, sessions, progress and logging."
            ),
        ).setup(app, "/console")
        logging.info("console on http://127.0.0.1:8080/console")
        web.run_app(app, host="127.0.0.1", port=8080)
