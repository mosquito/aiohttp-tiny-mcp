"""HTTP and stdio entry points. Settings precedence: CLI, DOCKER_MCP_* environment, config file."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import argclass
from aiodocker import Docker
from aiohttp import web
from aiohttp_tiny_mcp import Endpoint, Registry, run_stdio
from aiohttp_tiny_mcp.console import Console
from aiohttp_tiny_mcp.sqlite import SqliteHub, SqliteSessionStore, SqliteStorage

from .consent import LEVELS, Policy
from .events import watch
from .server import build

log = logging.getLogger("docker_mcp")


class Parser(argclass.Parser):
    """docker-mcp -- an MCP server for Docker."""

    state: Path = argclass.Argument(
        default=Path("docker-mcp.sqlite3"),
        help="SQLite file holding sessions and events. Several workers may share one.",
    )
    docker_url: str = argclass.Argument(
        default="",
        help="Where the daemon is. Empty means the usual socket for this machine.",
    )
    stdio: bool = argclass.Argument(
        default=False,
        help="Talk over standard input and output instead of listening on a port.",
    )
    host: str = argclass.Argument(default="127.0.0.1", help="Address to listen on.")
    port: int = argclass.Argument(default=8080, help="Port to listen on.")
    path: str = argclass.Argument(default="/mcp", help="Path to mount the endpoint at.")
    console: str = argclass.Argument(
        default="/console",
        help="Path to mount the web console at. Empty turns it off.",
    )
    allowed_origin: list[str] = argclass.Argument(
        default=[],
        nargs="*",
        help="Browser origins to accept. Any Origin not listed is refused.",
    )
    confirm: str = argclass.Argument(
        default="destructive",
        choices=LEVELS,
        help=(
            "What to ask a person about. 'destructive' asks only before what "
            "cannot be undone; 'never' asks nothing, for a host that already "
            "asks its own permission; 'changes' asks before anything that "
            "changes anything."
        ),
    )
    session_ttl: int = argclass.Argument(default=3600, help="Seconds a session lives without use.")
    log_level: str = argclass.Argument(
        default="info",
        choices=("debug", "info", "warning", "error"),
        help="How much this server writes to its own log.",
    )


def assemble(parser: Parser) -> tuple[Registry, SqliteStorage, Docker]:
    """Build the server from settings, independently of command-line parsing."""
    storage = SqliteStorage(parser.state)
    client = Docker(url=parser.docker_url) if parser.docker_url else Docker()
    registry = Registry(
        "docker",
        "0.1.0",
        hub=SqliteHub(storage),
        session_store=SqliteSessionStore(storage),
        session_ttl_seconds=parser.session_ttl,
    )
    return build(registry, client, Policy(parser.confirm)), storage, client


async def background(registry: Registry, storage: SqliteStorage, client: Docker):
    """Manage background tasks for the application lifetime.

    `sweeping` removes expired sessions and stale events. One file means
    workers on one machine, and a few of them deleting the same rows costs
    little.
    """
    tasks = [
        asyncio.ensure_future(storage.sweeping()),
        asyncio.ensure_future(watch(registry, client)),
    ]
    return tasks


async def serve(parser: Parser) -> None:
    registry, storage, client = assemble(parser)
    endpoint = Endpoint(registry, allowed_origins=set(parser.allowed_origin) or None)
    app = endpoint.app(parser.path)
    if parser.console:
        Console(
            parser.path,
            title="Docker",
            description=(
                "Inspect and control the Docker daemon this server is pointed "
                "at. Anything that removes, writes or creates asks first."
            ),
        ).setup(app, parser.console)

    async def start(_: web.Application):
        tasks = await background(registry, storage, client)
        yield
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await client.close()
        await storage.close()

    app.cleanup_ctx.append(start)
    log.info("listening on http://%s:%s%s", parser.host, parser.port, parser.path)
    if parser.console:
        log.info("console on http://%s:%s%s", parser.host, parser.port, parser.console)
    await web._run_app(app, host=parser.host, port=parser.port, print=None)


async def stdio(parser: Parser) -> None:
    registry, storage, client = assemble(parser)
    tasks = await background(registry, storage, client)
    try:
        await run_stdio(registry)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await client.close()
        await storage.close()


def main() -> None:
    parser = Parser(
        auto_env_var_prefix="DOCKER_MCP_",
        config_files=["docker-mcp.ini", "~/.config/docker-mcp.ini"],
    )
    parser.parse_args()
    logging.basicConfig(level=parser.log_level.upper(), stream=None)
    asyncio.run(stdio(parser) if parser.stdio else serve(parser))


if __name__ == "__main__":
    main()
