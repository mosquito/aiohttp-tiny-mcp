"""HTTP and stdio entry points. Settings precedence: CLI, DOCKER_MCP_* environment, config file."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path

import argclass
from aiodocker import Docker
from aiohttp import web
from aiohttp_tiny_mcp import Endpoint, Principal, Registry, run_stdio
from aiohttp_tiny_mcp.auth import Authorization
from aiohttp_tiny_mcp.auth.jwt import HMACJWTVerifier
from aiohttp_tiny_mcp.console import Console
from aiohttp_tiny_mcp.storage.sqlite import SqliteHub, SqliteSessionStore, SqliteStorage

from .consent import LEVELS, Policy
from .events import watch
from .server import build

log = logging.getLogger("docker_mcp")


@dataclass(frozen=True)
class QueryToken(Authorization):
    """Bearer authentication that also reads the `token` query argument.

    A client that cannot send a header sends the token in the URL instead. The
    URL is then a credential: it reaches browser history, the client's saved
    settings and every log that keeps the query. The header wins where both
    are present.
    """

    query: bool = False

    async def authenticate(self, request: web.Request) -> Principal:
        if self.query and "Authorization" not in request.headers:
            supplied = request.query.get("token", "")
            if supplied:
                return await self.principal(f"Bearer {supplied}")
        return await super().authenticate(request)


class JWT(argclass.Group):
    """HS256 JWT authentication with a pre-shared key."""

    psk: str = argclass.Secret(
        default="",
        help=(
            "Pre-shared key, at least 32 bytes. Prefer the config file or the "
            "environment variable: another user on this machine can read a command line."
        ),
    )
    audience: str = argclass.Argument(
        default="",
        help=(
            "Public URL of this endpoint. A token must name it in 'aud', and it is "
            "published as the protected resource. Required with a pre-shared key."
        ),
    )
    issuer: str = argclass.Argument(
        default="",
        help="Required 'iss' claim. Empty accepts a token from any issuer.",
    )
    query_token: bool = argclass.Argument(
        default=False,
        action=argclass.Actions.STORE_TRUE,
        help=(
            "Also accept the token as the 'token' query argument, for a client "
            "that sends no header. The access log masks that argument; the URL "
            "still reaches browser history and client settings."
        ),
    )


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
    unprivileged: bool = argclass.Argument(
        default=False,
        help="Forbid host mounts and privileged container access; allow named local volumes.",
    )
    network: str = argclass.Argument(
        default="",
        help="Fix the container network and disable network management tools.",
    )
    stdio: bool = argclass.Argument(
        default=False,
        help="Talk over standard input and output instead of listening on a port.",
    )
    jwt = JWT(
        title="JWT authentication",
        description="Verify an HS256 JWT on every HTTP request. Without a key, nothing is asked.",
    )
    host: str = argclass.Argument(default="127.0.0.1", help="Address to listen on.")
    port: int = argclass.Argument(default=8080, help="Port to listen on.")
    path: str = argclass.Argument(default="/mcp", help="Path to mount the endpoint at.")
    console: str = argclass.Argument(
        default="/console",
        help="Path to mount the web console at. Empty turns it off.",
    )
    allowed_origin: frozenset[str] = argclass.Argument(
        default=[],
        nargs=argclass.Nargs.ZERO_OR_MORE,
        type=str,
        converter=frozenset,
        help=(
            "Browser origins to accept. Any Origin not listed is refused. The "
            "environment variable and the config file hold a Python list, such "
            'as ["https://docker.example.com"].'
        ),
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
    generate_config = argclass.Argument(
        action=argclass.GenerateConfigAction,
        generator=argclass.INIConfigGenerator,
        metavar="FILE",
        help=(
            "Write every setting and its default to a config file, then exit. "
            "'-' writes to standard output."
        ),
    )
    log_level: str = argclass.Argument(
        default="info",
        choices=("debug", "info", "warning", "error"),
        help="How much this server writes to its own log.",
    )


def authentication(parser: Parser) -> Authorization | None:
    """Build the Bearer policy from the pre-shared key, or nothing without a key.

    The audience is the endpoint's public URL. It is what a token must name, and
    what the endpoint publishes as its protected resource.
    """
    key = str(parser.jwt.psk)
    if not key:
        return None
    if not parser.jwt.audience:
        raise SystemExit("--jwt-audience is required with a pre-shared key")
    verifier = HMACJWTVerifier(key, issuer=parser.jwt.issuer, audience=parser.jwt.audience)
    return QueryToken(verifier, resource=parser.jwt.audience, query=parser.jwt.query_token)


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
        auth=authentication(parser),
    )
    return (
        build(
            registry,
            client,
            Policy(parser.confirm),
            unprivileged=parser.unprivileged,
            network=parser.network,
        ),
        storage,
        client,
    )


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
    endpoint = Endpoint(
        registry,
        allowed_origins=parser.allowed_origin or None,
        # Behind a reverse proxy, uncomment this and let the proxy check Origin.
        # trust_proxy_origin_validation=True,
    )
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
    if registry.auth is not None:
        # One process, one caller, no HTTP request to carry a token.
        log.warning("the pre-shared key authenticates HTTP requests only; stdio ignores it")
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
        # A later file overrides an earlier one, and a missing file is skipped:
        # the machine's file, then this user's, then this directory's.
        config_files=["/etc/docker/mcp.ini", "~/.config/docker/mcp.ini", ".docker-mcp.ini"],
        config_argument="--config",
    )
    parser.parse_args()
    logging.basicConfig(level=parser.log_level.upper(), stream=None)
    asyncio.run(stdio(parser) if parser.stdio else serve(parser))


if __name__ == "__main__":
    main()
