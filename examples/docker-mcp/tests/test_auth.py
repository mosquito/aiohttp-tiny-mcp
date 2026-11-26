"""The pre-shared key, from the command line to a refused request."""

from __future__ import annotations

import time

import jwt
import pytest
from aiodocker import Docker
from aiohttp import ClientSession, web
from aiohttp_tiny_mcp import Client, Endpoint, Registry
from aiohttp_tiny_mcp.protocol.selection import AdapterSet
from aiohttp_tiny_mcp.storage.sqlite import SqliteHub, SqliteSessionStore, SqliteStorage

from docker_mcp.__main__ import Parser, authentication
from docker_mcp.server import build

pytestmark = pytest.mark.asyncio

MODERN = AdapterSet.default().by_version["2026-07-28"]
KEY = "a-pre-shared-key-of-at-least-32-bytes"
AUDIENCE = "https://docker.example.com/mcp"
ISSUER = "https://docker.example.com"


def settings(*arguments: str) -> Parser:
    parser = Parser()
    parser.parse_args(list(arguments))
    return parser


def token(**claims) -> str:
    """Mint a token the configured policy accepts, before any override."""
    return jwt.encode(
        {
            "sub": "tester",
            "iss": ISSUER,
            "aud": AUDIENCE,
            "exp": time.time() + 60,
            **claims,
        },
        KEY,
        algorithm="HS256",
    )


async def test_no_key_leaves_the_endpoint_open():
    assert authentication(settings()) is None


async def test_the_key_verifies_a_token():
    auth = authentication(
        settings("--jwt-psk", KEY, "--jwt-audience", AUDIENCE, "--jwt-issuer", ISSUER)
    )
    assert auth is not None
    principal = await auth.principal(f"Bearer {token()}")
    assert principal.subject == "tester"
    assert auth.metadata()["resource"] == AUDIENCE


async def test_another_issuer_is_refused():
    auth = authentication(
        settings("--jwt-psk", KEY, "--jwt-audience", AUDIENCE, "--jwt-issuer", ISSUER)
    )
    assert auth is not None
    assert await auth.verifier.verify(token(iss="https://elsewhere.example.com")) is None
    assert await auth.verifier.verify(token(aud="https://elsewhere.example.com/mcp")) is None
    assert await auth.verifier.verify(jwt.encode({"sub": "x"}, KEY, algorithm="HS256")) is None


async def test_the_query_argument_is_refused_unless_it_is_enabled():
    auth = authentication(
        settings("--jwt-psk", KEY, "--jwt-audience", AUDIENCE, "--jwt-issuer", ISSUER)
    )
    assert auth is not None and auth.query is False


async def test_what_the_settings_must_say():
    with pytest.raises(SystemExit, match="--jwt-audience"):
        authentication(settings("--jwt-psk", KEY))
    with pytest.raises(ValueError, match="32 bytes"):
        authentication(settings("--jwt-psk", "too-short", "--jwt-audience", AUDIENCE))


async def serve(storage: SqliteStorage, client: Docker, *arguments: str):
    """Start the endpoint of the example behind the pre-shared key."""
    registry = build(
        Registry(
            "docker",
            "0.1.0",
            hub=SqliteHub(storage, look_again=0.01),
            session_store=SqliteSessionStore(storage),
            auth=authentication(
                settings(
                    "--jwt-psk", KEY, "--jwt-audience", AUDIENCE, "--jwt-issuer", ISSUER, *arguments
                )
            ),
        ),
        client,
    )
    runner = web.AppRunner(Endpoint(registry).app("/mcp"))
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    host, port = runner.addresses[0]
    return runner, f"http://{host}:{port}/mcp"


@pytest.fixture
async def protected(storage: SqliteStorage, client: Docker):
    runner, url = await serve(storage, client)
    try:
        yield url
    finally:
        await runner.cleanup()


@pytest.fixture
async def with_query_token(storage: SqliteStorage, client: Docker):
    runner, url = await serve(storage, client, "--jwt-query-token")
    try:
        yield url
    finally:
        await runner.cleanup()


async def test_a_request_without_a_token_is_refused(protected: str):
    async with ClientSession() as http, http.post(protected, json={}) as response:
        assert response.status == 401
        assert "Bearer" in response.headers["WWW-Authenticate"]


async def test_a_token_opens_the_tools(protected: str):
    headers = {"Authorization": f"Bearer {token()}"}
    async with ClientSession(headers=headers) as http:
        async with Client(protected, MODERN, session=http) as remote:
            await remote.initialize()
            assert "containers" in {tool.name for tool in await remote.list_tools()}


async def test_the_query_argument_carries_the_token(with_query_token: str):
    async with ClientSession() as http:
        async with http.post(f"{with_query_token}?token={token()}", json={}) as response:
            assert response.status != 401
        async with http.post(f"{with_query_token}?token=not-a-token", json={}) as response:
            assert response.status == 401


async def test_the_header_wins_over_the_query_argument(with_query_token: str):
    headers = {"Authorization": "Bearer not-a-token"}
    async with ClientSession(headers=headers) as http:
        async with http.post(f"{with_query_token}?token={token()}", json={}) as response:
            assert response.status == 401


async def test_the_query_argument_is_ignored_by_default(protected: str):
    async with ClientSession() as http:
        async with http.post(f"{protected}?token={token()}", json={}) as response:
            assert response.status == 401
