"""A credential read from the URL, where a client cannot send a header."""

from __future__ import annotations

import logging
from base64 import b64encode

import pytest
from aiohttp import web
from aiohttp.log import access_logger
from aiohttp.test_utils import TestClient, TestServer, make_mocked_request

from aiohttp_tiny_mcp import (
    Authorization,
    Endpoint,
    Header,
    Principal,
    Query,
    Registry,
    SseEndpoint,
    StaticBasicAuth,
)
from aiohttp_tiny_mcp.auth import StaticVerifier, Unauthorized, read_credential
from aiohttp_tiny_mcp.storage.sessions import SESSION_HEADER
from tests.auth.helpers import RESOURCE, build, registry_for

# `asyncio_mode = auto` runs the async tests here, and the rest are plain functions.

CALL = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "who"}}

HEADERS = {"Accept": "application/json, text/event-stream"}

STANDARD = "access_token"
BEARER_SOURCES = (Header("Bearer"), Query(STANDARD))
BASIC_SOURCES = (Header("Basic"), Query("auth"))


def bearer(**options) -> Authorization:
    return Authorization(verifier=StaticVerifier({}), resource=RESOURCE, **options)


def basic(username: str = "alice", password: str = "secret") -> str:
    return b64encode(f"{username}:{password}".encode()).decode()


async def served(auth) -> TestClient:
    app = Endpoint(registry_for(auth)).app()
    SseEndpoint(registry_for(auth)).setup(app, metadata=False)
    return TestClient(TestServer(app))


def test_the_default_reads_the_header_alone():
    assert bearer().sources == (Header("Bearer"),)
    assert StaticBasicAuth(("alice", "secret")).sources == (Header("Basic"),)
    assert bearer().metadata()["bearer_methods_supported"] == ["header"]


def test_the_metadata_reports_the_configured_methods():
    assert bearer(sources=BEARER_SOURCES).metadata()["bearer_methods_supported"] == [
        "header",
        "query",
    ]
    assert bearer(sources=(Query(STANDARD),)).metadata()["bearer_methods_supported"] == ["query"]


@pytest.mark.parametrize(
    ("authorization", "error"),
    [
        ("Bearer from-header", None),
        ("Bearer wrong", "invalid_token"),
        ("Basic wrong", "invalid_request"),
        ("Bearer", "invalid_request"),
    ],
)
async def test_the_bearer_header_wins_over_the_url(authorization, error):
    policy = Authorization(
        StaticVerifier(
            {"from-header": Principal(subject="header"), "from-url": Principal(subject="url")}
        ),
        RESOURCE,
        sources=BEARER_SOURCES,
    )
    request = make_mocked_request(
        "GET", f"/mcp?{STANDARD}=from-url", headers={"Authorization": authorization}
    )
    if error is None:
        assert (await policy.authenticate(request)).subject == "header"
    else:
        with pytest.raises(Unauthorized) as caught:
            await policy.authenticate(request)
        assert caught.value.error == error


@pytest.mark.parametrize("authorization", ["Bearer wrong", "Basic", "Basic wrong extra"])
async def test_a_malformed_basic_header_prevents_query_fallback(authorization):
    policy = StaticBasicAuth(("alice", "secret"), sources=BASIC_SOURCES)
    request = make_mocked_request(
        "GET", f"/mcp?auth={basic()}", headers={"Authorization": authorization}
    )
    with pytest.raises(Unauthorized) as caught:
        await policy.authenticate(request)
    assert caught.value.error == "invalid_credentials"


async def test_sources_are_called_in_order_until_one_returns_a_value():
    policy = Authorization(
        StaticVerifier({"from-url": Principal(subject="url")}),
        RESOURCE,
        sources=(Query("first"), Query(STANDARD), Header("Bearer")),
    )
    request = make_mocked_request(
        "GET", f"/mcp?first=&{STANDARD}=from-url", headers={"Authorization": "Basic wrong"}
    )
    assert (await policy.authenticate(request)).subject == "url"


def test_a_source_receives_the_original_request():
    request = make_mocked_request("POST", "/mcp")

    def source(received: web.Request) -> str:
        assert received is request
        return "token"

    assert read_credential(request, (source,)) == "token"


@pytest.mark.parametrize("target", [f"/mcp?{STANDARD}=", "/mcp?other=value", "/mcp"])
def test_an_absent_or_empty_argument_reads_nothing(target):
    request = make_mocked_request("GET", target)
    assert Query(STANDARD)(request) is None


async def test_a_basic_value_keeps_its_plus_signs():
    password = "\u5bc6\u7801"
    encoded = basic(password=password)
    assert "+" in encoded
    policy = StaticBasicAuth(("alice", password), sources=BASIC_SOURCES)
    for literal in (encoded, encoded.replace("+", "%2B")):
        request = make_mocked_request("GET", f"/mcp?auth={literal}")
        principal = await policy.authenticate(request)
        assert principal is not None and principal.subject == "alice"

    plain = make_mocked_request("GET", f"/mcp?auth={encoded}")
    assert Query("auth")(plain) == encoded.replace("+", " ")


async def test_a_callable_can_replace_the_default_source():
    def api_key(request: web.Request) -> str | None:
        return request.headers.get("X-API-Key")

    policy = Authorization(
        StaticVerifier({"alice": Principal(subject="alice")}),
        RESOURCE,
        authorization_servers=("https://oauth.example.com",),
        sources=(api_key,),
    )
    async with TestClient(TestServer(Endpoint(registry_for(policy)).app())) as client:
        response = await client.post(
            "/mcp",
            headers={**HEADERS, "X-API-Key": "alice", "Authorization": "Bearer wrong"},
            json=CALL,
        )
        assert response.status == 200
        assert "result" in await response.json()
        rejected = await client.post(
            "/mcp", headers={**HEADERS, "Authorization": "Bearer alice"}, json=CALL
        )
        assert rejected.status == 401
        challenge = rejected.headers["WWW-Authenticate"]
        assert challenge.startswith("Bearer ")
        assert f'resource_metadata="{policy.metadata_url}"' in challenge


@pytest.mark.parametrize("route", ["post-mcp", "get-sse"])
async def test_a_bearer_token_in_the_url_authenticates(route):
    registry, _ = build(sources=BEARER_SOURCES)
    app = Endpoint(registry).app()
    SseEndpoint(registry).setup(app, metadata=False)
    method, path = route.split("-", 1)
    async with TestClient(TestServer(app)) as client:
        response = await client.request(
            method,
            f"/{path}?{STANDARD}=alice",
            headers=HEADERS,
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        )
        assert response.status in (200, 202), await response.text()


async def test_a_basic_credential_in_the_url_authenticates():
    policy = StaticBasicAuth(("alice", "secret", ["read"]), sources=BASIC_SOURCES)
    async with TestClient(TestServer(Endpoint(registry_for(policy)).app())) as client:
        response = await client.post(
            f"/mcp?auth={basic()}",
            headers=HEADERS,
            json={**CALL, "params": {"name": "who", "arguments": {}}},
        )
        assert response.status == 200
        answered = await response.json()
        assert answered["result"]["content"][0]["text"].startswith("alice:")


async def test_an_invalid_credential_in_the_url_is_refused_with_the_policy_challenge():
    policy = StaticBasicAuth(("alice", "secret", ["read"]), sources=BASIC_SOURCES)
    async with TestClient(TestServer(Endpoint(registry_for(policy)).app())) as client:
        response = await client.post(
            f"/mcp?auth={basic(password='wrong')}",
            headers=HEADERS,
            json=CALL,
        )
        assert response.status == 401
        assert response.headers["WWW-Authenticate"] == 'Basic realm="mcp", charset="UTF-8"'


async def test_the_url_is_not_read_where_the_policy_declares_nothing():
    """The setting is opt-in, so the same request fails without it."""
    registry, _ = build()
    async with TestClient(TestServer(Endpoint(registry).app())) as client:
        response = await client.post(
            f"/mcp?{STANDARD}=alice",
            headers=HEADERS,
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        )
        assert response.status == 401


async def test_a_session_belongs_to_the_principal_whatever_carried_the_token():
    """Ownership follows the principal, not the place the credential came from."""
    registry, _ = build(sources=BEARER_SOURCES)
    async with TestClient(TestServer(Endpoint(registry).app())) as client:
        opened = await client.post(
            f"/mcp?{STANDARD}=alice",
            headers=HEADERS,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2025-11-25", "capabilities": {}},
            },
        )
        session = opened.headers[SESSION_HEADER]
        intruder = await client.post(
            f"/mcp?{STANDARD}=bob",
            headers={**HEADERS, SESSION_HEADER: session, "MCP-Protocol-Version": "2025-11-25"},
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        )
        assert intruder.status == 404


async def test_a_declared_argument_is_masked_in_the_access_log(
    caplog: pytest.LogCaptureFixture, restore_access_filters
) -> None:
    """A credential in a URL reaches every log on the way. This one does not."""
    caplog.set_level(logging.INFO, logger=access_logger.name)
    policy = StaticBasicAuth(("alice", "secret", ["read"]), sources=BASIC_SOURCES)
    app = web.Application()
    Endpoint(registry_for(policy)).setup(app)
    async with TestClient(TestServer(app)) as client:
        response = await client.get("/missing?auth=should-not-appear&token=nor-this")
        assert response.status == 404
        await response.read()
    records = [record for record in caplog.records if record.name == access_logger.name]
    assert records, "the access log recorded nothing"
    message = records[-1].getMessage()
    assert "should-not-appear" not in message
    assert "nor-this" not in message
    assert "auth=[REDACTED]" in message


def test_the_registry_reports_every_declared_argument_once():
    registry = Registry(
        "named",
        "1",
        auth=[
            StaticBasicAuth(("alice", "secret"), sources=(*BASIC_SOURCES, Query("token"))),
            bearer(sources=(Query("token"),)),
        ],
    )
    assert registry.credential_arguments == ("auth", "token")


async def test_both_halves_of_the_legacy_transport_read_the_url():
    """That transport authenticates the stream and every message, so both read it.

    The address the stream names carries no credential: a server does not echo one
    back. A client that authenticates by URL appends its own. The stream stays open
    while the messages are posted, because closing it ends the session.
    """
    registry, _ = build(sources=BEARER_SOURCES)
    app = web.Application()
    SseEndpoint(registry).setup(app)
    async with TestClient(TestServer(app)) as client:
        refused = await client.get("/sse", headers={"Accept": "text/event-stream"})
        assert refused.status == 401
        refused.close()

        stream = await client.get(f"/sse?{STANDARD}=alice", headers={"Accept": "text/event-stream"})
        assert stream.status == 200
        try:
            assert (await stream.content.readline()).strip() == b"event: endpoint"
            address = (await stream.content.readline()).decode().partition("data:")[2].strip()

            without = await client.post(address, headers=HEADERS, json=CALL)
            assert without.status == 401
            with_token = await client.post(
                f"{address}&{STANDARD}=alice", headers=HEADERS, json=CALL
            )
            assert with_token.status == 202
        finally:
            stream.close()
