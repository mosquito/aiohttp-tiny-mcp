"""What a resource server owes a caller, and what it refuses.

Four things are checked here, and each one fails open if it is not: a request
without a usable token is refused in a way that says where to get one; a tool
that names a scope is unreachable without it; a session belongs to whoever
opened it; and the namespace a caller's state lands in comes from what was
verified rather than from what the caller said.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from pydantic import BaseModel

from aiohttp_tiny_mcp import Client, Endpoint, MemoryHub, MemorySessionStore, Registry
from aiohttp_tiny_mcp.auth import Authorization, Principal, StaticVerifier, Unauthorized
from aiohttp_tiny_mcp.namespaces import current, namespace
from aiohttp_tiny_mcp.protocol.selection import AdapterSet
from aiohttp_tiny_mcp.sessions import SESSION_HEADER
from aiohttp_tiny_mcp.testing import over_http, serving

pytestmark = pytest.mark.asyncio

MODERN = AdapterSet.default().by_version["2026-07-28"]
LEGACY = AdapterSet.default().by_version["2025-11-25"]

RESOURCE = "https://mcp.example.com/reports/mcp"

TOKENS = {
    "alice": Principal(
        subject="alice",
        client_id="reports",
        issuer="https://login.example.com",
        scopes=frozenset({"reports:read"}),
    ),
    "bob": Principal(
        subject="bob",
        client_id="reports",
        issuer="https://login.example.com",
        scopes=frozenset({"reports:read"}),
    ),
    "unscoped": Principal(
        subject="carol",
        client_id="reports",
        issuer="https://login.example.com",
        scopes=frozenset(),
    ),
    "stale": Principal(
        subject="dave",
        client_id="reports",
        issuer="https://login.example.com",
        scopes=frozenset({"reports:read"}),
        expires_at=time.time() - 60,
    ),
}


class Nothing(BaseModel):
    pass


def build(**options) -> tuple[Registry, StaticVerifier]:
    verifier = StaticVerifier(TOKENS)
    auth = Authorization(
        verifier=verifier,
        resource=RESOURCE,
        authorization_servers=["https://login.example.com"],
        scopes_supported=["reports:read"],
        **options,
    )
    registry = Registry(
        "reports", "1.0", hub=MemoryHub(), session_store=MemorySessionStore(), auth=auth
    )

    async def report(args: Nothing, principal: Principal) -> str:
        """Return the caller's report."""
        return f"report for {principal.subject}"

    async def public(args: Nothing) -> str:
        """Anyone holding a token may call this."""
        return "open"

    registry.tool(report, scopes=["reports:read"])
    registry.tool(public)
    return registry, verifier


@pytest.fixture
async def served() -> AsyncIterator[TestClient]:
    registry, _ = build()
    server = TestServer(Endpoint(registry).app("/mcp"))
    client = TestClient(server)
    await client.start_server()
    try:
        yield client
    finally:
        await client.close()


ANYTHING = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}

HEADERS = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}


def calling(registry: Registry, token: str | None):
    """A real client, carrying a token on every request."""
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return over_http(registry, adapter=MODERN, headers=headers)


async def test_a_request_with_no_token_is_refused(served):
    answered = await served.post("/mcp", json=ANYTHING, headers=HEADERS)
    assert answered.status == 401


async def test_the_refusal_says_where_to_get_a_token(served):
    """A client that has no token learns only that it was refused, unless the
    challenge points at the metadata."""
    answered = await served.post("/mcp", json=ANYTHING, headers=HEADERS)
    challenge = answered.headers["WWW-Authenticate"]
    assert challenge.startswith("Bearer ")
    assert 'error="invalid_request"' in challenge
    assert "https://mcp.example.com/.well-known/oauth-protected-resource/reports/mcp" in challenge


async def test_a_token_nobody_issued_is_refused(served):
    answered = await served.post(
        "/mcp", json=ANYTHING, headers={**HEADERS, "Authorization": "Bearer nonsense"}
    )
    assert answered.status == 401
    assert 'error="invalid_token"' in answered.headers["WWW-Authenticate"]


async def test_an_expired_token_is_refused(served):
    answered = await served.post(
        "/mcp", json=ANYTHING, headers={**HEADERS, "Authorization": "Bearer stale"}
    )
    assert answered.status == 401


async def test_a_scheme_that_is_not_bearer_is_refused(served):
    answered = await served.post(
        "/mcp", json=ANYTHING, headers={**HEADERS, "Authorization": "Basic YWxpY2U6"}
    )
    assert answered.status == 401


async def test_a_valid_token_gets_through():
    registry, _ = build()
    async with calling(registry, "alice") as client:
        result = await client.call_tool("public", {})
    assert "open" in result.content[0].text


async def test_the_metadata_says_what_a_client_needs():
    """RFC 9728, below the resource's own path so two servers on one host do
    not collide."""
    registry, _ = build()
    server = TestServer(Endpoint(registry).app("/mcp"))
    client = TestClient(server)
    await client.start_server()
    try:
        answered = await client.get("/.well-known/oauth-protected-resource/reports/mcp")
        assert answered.status == 200
        found = await answered.json()
    finally:
        await client.close()

    assert found["resource"] == RESOURCE
    assert found["authorization_servers"] == ["https://login.example.com"]
    assert found["scopes_supported"] == ["reports:read"]
    assert found["bearer_methods_supported"] == ["header"]


async def test_no_metadata_route_where_nothing_is_verified():
    """A server that verifies nothing must not advertise that it does."""
    registry = Registry("open", "1.0", hub=MemoryHub(), session_store=MemorySessionStore())
    server = TestServer(Endpoint(registry).app("/mcp"))
    client = TestClient(server)
    await client.start_server()
    try:
        answered = await client.get("/.well-known/oauth-protected-resource")
        assert answered.status == 404
    finally:
        await client.close()


async def test_a_missing_scope_is_a_result_and_names_what_is_missing():
    """A protocol error may not reach the model. A result does, and one that
    says which scope is missing can be acted on."""
    registry, _ = build()
    async with calling(registry, "unscoped") as client:
        result = await client.call_tool("report", {})
    assert result.is_error is True
    assert "reports:read" in result.content[0].text


async def test_a_held_scope_reaches_the_handler():
    registry, _ = build()
    async with calling(registry, "alice") as client:
        result = await client.call_tool("report", {})
    assert result.is_error is False
    assert "report for alice" in result.content[0].text


async def test_a_server_wide_scope_is_refused_before_any_tool():
    """`required_scopes` is a property of the request, so it fails the request
    rather than one call inside it."""
    registry, _ = build(required_scopes=["reports:read"])
    server = TestServer(Endpoint(registry).app("/mcp"))
    client = TestClient(server)
    await client.start_server()
    try:
        answered = await client.post(
            "/mcp", json=ANYTHING, headers={**HEADERS, "Authorization": "Bearer unscoped"}
        )
        assert answered.status == 403
        assert 'error="insufficient_scope"' in answered.headers["WWW-Authenticate"]
    finally:
        await client.close()


async def test_a_scoped_tool_is_unreachable_without_a_verified_caller():
    """Over a transport that carries no token there is nobody to check, and
    allowing the call would make the scope a comment."""
    from aiohttp_tiny_mcp.dispatcher import refusal

    registry, _ = build()
    refused = refusal(registry.tools["report"], None)
    assert refused is not None
    assert "no verified caller" in refused
    assert refusal(registry.tools["public"], None) is None


async def test_scopes_without_a_verifier_are_refused_at_registration():
    registry = Registry("open", "1.0", hub=MemoryHub(), session_store=MemorySessionStore())

    async def guarded(args: Nothing) -> str:
        """Declares a scope nothing will ever check."""
        return "no"

    with pytest.raises(TypeError, match="never runs"):
        registry.tool(guarded, scopes=["reports:read"])


async def test_a_principal_without_a_verifier_is_refused_at_registration():
    registry = Registry("open", "1.0", hub=MemoryHub(), session_store=MemorySessionStore())

    async def who(args: Nothing, principal: Principal) -> str:
        """Wants a caller nobody checked."""
        return "no"

    with pytest.raises(TypeError, match="no auth="):
        registry.tool(who)


async def opened(client: TestClient, token: str) -> str:
    """Shake hands on a revision that keeps a session, and keep its id."""
    answered = await client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "t", "version": "0"},
            },
        },
        headers={**HEADERS, "Authorization": f"Bearer {token}"},
    )
    assert answered.status == 200
    return answered.headers[SESSION_HEADER]


async def test_a_session_belongs_to_whoever_opened_it(served):
    """A session id travels in a header, so a copied one is a credential.
    Another principal holding it is answered as if it had expired."""
    session_id = await opened(served, "alice")
    mine = await served.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        headers={
            **HEADERS,
            "Authorization": "Bearer alice",
            SESSION_HEADER: session_id,
            "MCP-Protocol-Version": "2025-11-25",
        },
    )
    assert mine.status == 200

    stolen = await served.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 3, "method": "tools/list", "params": {}},
        headers={
            **HEADERS,
            "Authorization": "Bearer bob",
            SESSION_HEADER: session_id,
            "MCP-Protocol-Version": "2025-11-25",
        },
    )
    assert stolen.status in (200, 400, 404)
    assert session_id not in await stolen.text()


async def test_binding_can_be_turned_off_for_a_layer_that_does_it_elsewhere():
    """The namespace separates sessions by principal as well, so it is turned
    off with the binding; otherwise bob's lookup of alice's session is a 404."""
    registry, _ = build(bind_sessions=False, namespace_from_token=False)
    server = TestServer(Endpoint(registry).app("/mcp"))
    client = TestClient(server)
    await client.start_server()
    try:
        session_id = await opened(client, "alice")
        answered = await client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            headers={
                **HEADERS,
                "Authorization": "Bearer bob",
                SESSION_HEADER: session_id,
                "MCP-Protocol-Version": "2025-11-25",
            },
        )
        assert answered.status == 200
    finally:
        await client.close()


async def test_the_namespace_comes_from_the_token(served):
    """Two principals calling the same server must not reach one another's
    sessions, and the name that separates them is the verified one."""
    first = await opened(served, "alice")
    second = await opened(served, "bob")
    assert first != second

    answered = await served.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        headers={
            **HEADERS,
            "Authorization": "Bearer bob",
            SESSION_HEADER: first,
            "MCP-Protocol-Version": "2025-11-25",
        },
    )
    assert first not in await answered.text()


async def test_an_application_namespace_is_not_overwritten():
    """A deployment that decides tenancy itself keeps its answer."""
    registry, _ = build()
    seen: list[str | None] = []

    @web.middleware
    async def tenant(request: web.Request, handler):
        namespace.set("chosen-by-the-application")
        return await handler(request)

    async def sees(args: Nothing) -> str:
        """Report the namespace in force."""
        seen.append(current())
        return "noted"

    registry.tool(sees)
    async with serving(registry, middlewares=[tenant]) as url:
        async with aiohttp.ClientSession(headers={"Authorization": "Bearer alice"}) as session:
            async with Client(url, MODERN, session=session) as client:
                await client.initialize()
                await client.call_tool("sees", {})

    assert seen == ["chosen-by-the-application"]


async def test_the_principal_names_the_issuer_and_the_subject():
    """Two subjects with the same name from different issuers are two
    principals, so the identity carries both."""
    one = Principal(subject="alice", issuer="https://a.example.com")
    two = Principal(subject="alice", issuer="https://b.example.com")
    assert one.identity != two.identity


async def test_a_machine_token_still_has_an_identity():
    """A token naming no user names the client it was issued to."""
    machine = Principal(client_id="batch", issuer="https://a.example.com")
    assert "batch" in machine.identity


async def test_a_refusal_is_raised_rather_than_returned():
    """Every caller of this has to refuse the same way, and a return value
    that is forgotten fails open."""
    auth = Authorization(verifier=StaticVerifier(TOKENS), resource=RESOURCE)
    with pytest.raises(Unauthorized):
        await auth.principal(None)


async def test_a_client_reaches_a_guarded_server_with_a_token():
    """The whole path, through a real client."""
    registry, _ = build()
    async with calling(registry, "alice") as client:
        result = await client.call_tool("report", {})
    assert "report for alice" in result.content[0].text
