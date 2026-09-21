"""OAuth sign-in with a local provider, exact client binding, and shared storage."""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import replace

import pytest
from aiohttp import ClientSession, web
from aiohttp.test_utils import TestClient, TestServer
from pydantic import BaseModel
from yarl import URL

from aiohttp_tiny_mcp import Endpoint, Principal, Registry
from aiohttp_tiny_mcp.console import Console
from aiohttp_tiny_mcp.oauth import Identity, OAuthClient, OAuthFacade, UpstreamAuthError
from aiohttp_tiny_mcp.oauth.facade import challenge
from aiohttp_tiny_mcp.oauth.github import GitHub, github_identity

VERIFIER = "v" * 43


class Nothing(BaseModel):
    pass


@pytest.fixture
async def provider():
    app = web.Application()
    calls = []
    tokens = {"access_token": "github-secret", "token_type": "bearer", "scope": "read:user"}
    profile = {"id": 123, "login": "alice", "name": "Alice", "email": None}

    async def token(request):
        body = dict(await request.post())
        calls.append(body)
        assert request.headers["Accept"] == "application/json"
        assert body["client_id"] == "github-client"
        assert body["client_secret"] == "github-client-secret"
        assert body["grant_type"] == "authorization_code"
        return web.json_response(tokens)

    async def user(request):
        assert request.headers["Authorization"] == "Bearer github-secret"
        assert request.headers["User-Agent"] == "aiohttp-tiny-mcp"
        return web.json_response(profile)

    app.router.add_post("/token", token)
    app.router.add_get("/user", user)
    async with TestServer(app) as server:

        class RewriteSession:
            def __init__(self, http):
                self.http = http

            def request(self, method, url, **kwargs):
                assert url == "https://api.github.com/user"
                return self.http.request(method, server.make_url("/user"), **kwargs)

        async def identify(tokens, http):
            return await github_identity(tokens, RewriteSession(http))

        upstream = replace(
            GitHub("github-client", "github-client-secret"),
            authorize_url=str(server.make_url("/authorize")),
            token_url=str(server.make_url("/token")),
            profile=identify,
        )
        yield upstream, calls, tokens, profile


@pytest.fixture
async def protected(provider, unused_tcp_port):
    upstream, calls, tokens, profile = provider
    origin = f"http://127.0.0.1:{unused_tcp_port}"
    facade = OAuthFacade(
        origin + "/oauth",
        upstream,
        resource=origin + "/mcp",
        clients=[
            OAuthClient(
                "console", [origin + "/console/oauth-callback"], "Console", require_consent=True
            )
        ],
        scopes=["read"],
    )
    registry = Registry("private", "1", auth=facade.resource(required_scopes=["read"]))

    @registry.tool()
    async def who(args: Nothing, principal: Principal) -> str:
        return principal.subject or ""

    app = Endpoint(registry).app()
    facade.setup(app)
    Console(oauth_client_id="console").setup(app)
    async with TestClient(TestServer(app, port=unused_tcp_port)) as client:
        yield facade, client


def parameters(facade, **changes):
    return {
        "client_id": "console",
        "redirect_uri": facade.origin + "/console/oauth-callback",
        "response_type": "code",
        "resource": facade.resource_url,
        "code_challenge": challenge(VERIFIER),
        "code_challenge_method": "S256",
        "state": "client-state",
        "scope": "read",
        **changes,
    }


async def consent(facade, client, **changes):
    response = await client.get("/oauth/authorize", params=parameters(facade, **changes))
    assert response.status == 200, await response.text()
    text = await response.text()
    assert "Console" in text and "read" in text
    assert response.headers["Cache-Control"] == "no-store"
    # Chrome serializes the form's Origin as null with no-referrer.
    assert response.headers["Referrer-Policy"] == "same-origin"
    assert (
        str(URL(facade.upstream.authorize_url).origin())
        in response.headers["Content-Security-Policy"]
    )
    cookie = next(iter(response.cookies.values()))
    assert cookie["httponly"] and cookie["samesite"] == "Lax"
    return re.search(r'name="ticket" value="([^"]+)"', text)[1]


async def upstream_redirect(facade, client):
    ticket = await consent(facade, client)
    response = await client.post(
        "/oauth/authorize",
        data={"ticket": ticket, "decision": "allow"},
        headers={"Origin": facade.origin},
        allow_redirects=False,
    )
    assert response.status == 303, await response.text()
    target = URL(response.headers["Location"])
    assert target.query["redirect_uri"] == facade.callback_url
    assert target.query["code_challenge_method"] == "S256"
    assert target.query["state"] != "client-state"
    assert target.query["code_challenge"] != challenge(VERIFIER)
    return target


async def issued_code(facade, client):
    target = await upstream_redirect(facade, client)
    response = await client.get(
        "/oauth/callback",
        params={"state": target.query["state"], "code": "provider-code"},
        allow_redirects=False,
    )
    assert response.status == 303, await response.text()
    callback = URL(response.headers["Location"])
    assert callback.query["iss"] == facade.issuer
    assert callback.query["state"] == "client-state"
    assert "github-secret" not in response.headers["Location"]
    return callback.query["code"], target


def token_params(facade, code, **changes):
    return {
        "grant_type": "authorization_code",
        "client_id": "console",
        "code": code,
        "code_verifier": VERIFIER,
        "redirect_uri": facade.origin + "/console/oauth-callback",
        "resource": facade.resource_url,
        **changes,
    }


async def access_token(facade, client):
    code, _ = await issued_code(facade, client)
    response = await client.post("/oauth/token", data=token_params(facade, code))
    assert response.status == 200, await response.text()
    return await response.json()


async def test_registered_client_redirects_directly_by_default(protected, provider):
    facade, client = protected
    facade.clients["console"] = OAuthClient("console", [facade.origin + "/console/oauth-callback"])
    response = await client.get(
        "/oauth/authorize", params=parameters(facade), allow_redirects=False
    )
    assert response.status == 303
    assert response.headers["Cache-Control"] == "no-store"
    target = URL(response.headers["Location"])
    assert target.with_query(None) == URL(facade.upstream.authorize_url)
    assert target.query["redirect_uri"] == facade.callback_url
    assert target.query["code_challenge_method"] == "S256"
    assert target.query["code_challenge"] != challenge(VERIFIER)
    assert target.query["state"] != "client-state"
    cookie = next(iter(response.cookies.values()))
    assert cookie["httponly"] and cookie["samesite"] == "Lax"
    assert not any("/consent/" in key for key in facade.store.records)

    callback_params = {"state": target.query["state"], "code": "provider-code"}
    async with ClientSession() as stranger:
        denied = await stranger.get(facade.callback_url, params=callback_params)
        assert denied.status == 400
        assert (await denied.json())["error"] == "invalid_request"
    assert not provider[1]

    response = await client.get("/oauth/callback", params=callback_params, allow_redirects=False)
    assert response.status == 303
    callback = URL(response.headers["Location"])
    assert callback.query["state"] == "client-state"
    assert callback.query["iss"] == facade.issuer
    response = await client.post("/oauth/token", data=token_params(facade, callback.query["code"]))
    assert response.status == 200
    result = await response.json()
    principal = await facade.verify(result["access_token"])
    assert principal.subject == "github:123"
    assert principal.scopes == {"read"}


async def test_github_sign_in_produces_resource_bound_mcp_token(protected, provider):
    facade, client = protected
    metadata = await (await client.get("/.well-known/oauth-authorization-server/oauth")).json()
    assert metadata["issuer"] == facade.issuer
    assert metadata["code_challenge_methods_supported"] == ["S256"]
    assert metadata["grant_types_supported"] == ["authorization_code"]
    prm = await (await client.get("/.well-known/oauth-protected-resource/mcp")).json()
    assert prm["authorization_servers"] == [facade.issuer]
    result = await access_token(facade, client)
    token = result["access_token"]
    assert token != "github-secret"
    assert result["token_type"] == "Bearer" and 0 < result["expires_in"] <= 3600
    assert "refresh_token" not in result
    principal = await facade.verify(token)
    assert principal.subject == "github:123"
    assert principal.issuer == facade.issuer
    assert principal.client_id == "console"
    assert principal.scopes == {"read"}
    assert principal.claims["login"] == "alice"
    assert await facade.verify("github-secret") is None
    assert "github-secret" not in repr(facade.store.records)
    response = await client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "who",
                "arguments": {},
                "_meta": {
                    "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                    "io.modelcontextprotocol/clientCapabilities": {},
                    "io.modelcontextprotocol/clientInfo": {"name": "test", "version": "1"},
                },
            },
        },
        headers={
            "Authorization": f"Bearer {token}",
            "MCP-Protocol-Version": "2026-07-28",
            "Mcp-Method": "tools/call",
            "Mcp-Name": "who",
            "Accept": "application/json",
        },
    )
    assert response.status == 200, await response.text()
    assert (await response.json())["result"]["content"][0]["text"] == "github:123"
    await facade.revoke(token)
    assert await facade.verify(token) is None


@pytest.mark.parametrize(
    "changes,error",
    [
        ({"client_id": "unknown"}, "invalid_client"),
        ({"redirect_uri": "https://attacker.example/cb"}, "invalid_request"),
        ({"resource": "https://other.example/mcp"}, "invalid_target"),
        ({"code_challenge_method": "plain"}, "invalid_request"),
        ({"code_challenge": ""}, "invalid_request"),
        ({"scope": "admin"}, "invalid_scope"),
        ({"response_type": "token"}, "unsupported_response_type"),
    ],
)
async def test_invalid_authorization_requests(protected, changes, error):
    facade, client = protected
    response = await client.get(
        "/oauth/authorize", params=parameters(facade, **changes), allow_redirects=False
    )
    if response.status == 303:
        url = URL(response.headers["Location"])
        assert url.origin() == URL(facade.origin)
        assert url.query["error"] == error
        assert url.query["iss"] == facade.issuer
    else:
        assert response.status == 400
        assert (await response.json())["error"] == error
        assert "Location" not in response.headers


async def test_browser_binding_origin_and_consent_replay(protected, provider):
    facade, client = protected
    ticket = await consent(facade, client)
    data = {"ticket": ticket, "decision": "allow"}
    wrong = await client.post(
        "/oauth/authorize", data=data, headers={"Origin": "https://evil.test"}
    )
    assert wrong.status == 403
    async with ClientSession() as stranger:
        response = await stranger.post(
            facade.issuer + "/authorize", data=data, headers={"Origin": facade.origin}
        )
        assert response.status == 400
    response = await client.post(
        "/oauth/authorize", data=data, headers={"Origin": facade.origin}, allow_redirects=False
    )
    assert response.status == 303
    target = URL(response.headers["Location"])
    repeated = await client.post(
        "/oauth/authorize", data=data, headers={"Origin": facade.origin}, allow_redirects=False
    )
    assert repeated.status == 400
    query = {"state": target.query["state"], "code": "provider-code"}
    async with ClientSession() as stranger:
        response = await stranger.get(facade.callback_url, params=query, allow_redirects=False)
        assert response.status == 400
    assert not provider[1]
    first = await client.get("/oauth/callback", params=query, allow_redirects=False)
    assert first.status == 303
    repeated = await client.get("/oauth/callback", params=query, allow_redirects=False)
    assert repeated.status == 400
    assert len(provider[1]) == 1
    assert challenge(provider[1][0]["code_verifier"]) == target.query["code_challenge"]


async def test_denied_consent_never_contacts_provider(protected, provider):
    facade, client = protected
    ticket = await consent(facade, client)
    response = await client.post(
        "/oauth/authorize",
        data={"ticket": ticket, "decision": "deny"},
        headers={"Origin": facade.origin},
        allow_redirects=False,
    )
    query = URL(response.headers["Location"]).query
    assert query["error"] == "access_denied" and query["iss"] == facade.issuer
    assert not provider[1]


@pytest.mark.parametrize(
    "changes",
    [
        {"code_verifier": "x" * 43},
        {"client_id": "unknown"},
        {"resource": "https://other.example/mcp"},
        {"redirect_uri": "https://attacker.example/cb"},
        {"grant_type": "refresh_token"},
    ],
)
async def test_code_binding_rejects_invalid_exchange_without_consuming_code(protected, changes):
    facade, client = protected
    code, _ = await issued_code(facade, client)
    response = await client.post("/oauth/token", data=token_params(facade, code, **changes))
    assert response.status == 400
    valid = await client.post("/oauth/token", data=token_params(facade, code))
    assert valid.status == 200
    repeated = await client.post("/oauth/token", data=token_params(facade, code))
    assert repeated.status == 400


async def test_only_one_concurrent_code_exchange_succeeds(protected):
    facade, client = protected
    code, _ = await issued_code(facade, client)
    responses = await asyncio.gather(
        *(client.post("/oauth/token", data=token_params(facade, code)) for _ in range(8))
    )
    assert sorted(response.status for response in responses) == [200] + [400] * 7


async def test_shared_store_accepts_tokens_on_another_worker(protected, provider):
    facade, client = protected
    result = await access_token(facade, client)
    worker = OAuthFacade(
        facade.issuer,
        provider[0],
        resource=facade.resource_url,
        clients=list(facade.clients.values()),
        scopes=facade.scopes,
        store=facade.store,
    )
    assert (await worker.verify(result["access_token"])).subject == "github:123"
    other = OAuthFacade(
        facade.issuer,
        provider[0],
        resource=facade.origin + "/other",
        clients=list(facade.clients.values()),
        store=facade.store,
    )
    assert await other.verify(result["access_token"]) is None


async def test_expired_tokens_and_flows_are_rejected(protected, monkeypatch):
    facade, client = protected
    token = (await access_token(facade, client))["access_token"]
    ticket = await consent(facade, client)
    now = time.time()
    monkeypatch.setattr("aiohttp_tiny_mcp.oauth.facade.time.time", lambda: now + 7200)
    assert await facade.verify(token) is None
    response = await client.post(
        "/oauth/authorize",
        data={"ticket": ticket, "decision": "allow"},
        headers={"Origin": facade.origin},
        allow_redirects=False,
    )
    assert response.status == 400


async def test_custom_identity_mapper_controls_access_and_namespace(protected):
    facade, client = protected

    async def mapper(identity):
        assert identity == Identity(
            "123", "github", {"login": "alice", "name": "Alice", "email": None}
        )
        return Principal(subject="internal-user", namespace="team", scopes=frozenset({"read"}))

    facade.identity = mapper
    principal = await facade.verify((await access_token(facade, client))["access_token"])
    assert principal.subject == "internal-user" and principal.namespace == "team"
    facade.identity = lambda identity: None
    target = await upstream_redirect(facade, client)
    response = await client.get(
        "/oauth/callback",
        params={"state": target.query["state"], "code": "provider-code"},
        allow_redirects=False,
    )
    assert URL(response.headers["Location"]).query["error"] == "access_denied"


@pytest.mark.parametrize("bad", [True, None, 0, "123"])
async def test_github_rejects_invalid_stable_user_id(protected, provider, bad):
    facade, client = protected
    provider[3]["id"] = bad
    target = await upstream_redirect(facade, client)
    response = await client.get(
        "/oauth/callback",
        params={"state": target.query["state"], "code": "provider-code"},
        allow_redirects=False,
    )
    assert URL(response.headers["Location"]).query["error"] == "temporarily_unavailable"


async def test_optional_github_expiry_and_refresh_token_are_not_mcp_credentials(provider):
    upstream, _, data, _ = provider
    data.update(expires_in=28800, refresh_token="github-refresh-secret")
    async with ClientSession() as http:
        tokens = await upstream.exchange(
            http,
            code="provider-code",
            redirect_uri="https://mcp.test/oauth/callback",
            verifier=VERIFIER,
        )
    assert tokens.refresh_token == "github-refresh-secret"
    assert time.time() < tokens.expires_at <= time.time() + 28800
    assert "github-secret" not in repr(tokens)
    assert "github-refresh-secret" not in repr(tokens)
    assert "github-client-secret" not in repr(upstream)


@pytest.mark.parametrize("value", ["expired", 0, -1, True])
async def test_invalid_provider_expiry(provider, value):
    upstream, _, data, _ = provider
    data["expires_in"] = value
    async with ClientSession() as http:
        with pytest.raises(UpstreamAuthError):
            await upstream.exchange(
                http, code="code", redirect_uri="https://mcp.test/cb", verifier=VERIFIER
            )


async def test_public_route_integration_does_not_exempt_unrelated_routes(provider, unused_tcp_port):
    origin = f"http://127.0.0.1:{unused_tcp_port}"
    facade = OAuthFacade(
        origin + "/oauth",
        provider[0],
        resource=origin + "/mcp",
        clients=[OAuthClient("console", [origin + "/console/oauth-callback"])],
    )

    @web.middleware
    async def protect(request, handler):
        if Console.is_public(request) or OAuthFacade.is_public(request):
            return await handler(request)
        raise web.HTTPUnauthorized()

    app = web.Application(middlewares=[protect])
    facade.setup(app)
    Console().setup(app)
    async with TestClient(TestServer(app, port=unused_tcp_port)) as client:
        assert (await client.get("/.well-known/oauth-authorization-server/oauth")).status == 200
        assert (await client.get("/console/oauth-callback")).status == 200
        assert (await client.post("/oauth/token", data={})).status == 400
        for path in ("/mcp", "/oauth/unknown", "/oauth/callback/extra"):
            assert (await client.get(path)).status == 401
        assert (await client.post("/console/oauth-callback")).status == 401
        assert (await client.head("/oauth/callback")).status == 401


@pytest.mark.parametrize(
    "issuer",
    ["https://mcp.test/", "http://mcp.test/oauth", "https://u:p@mcp.test", "https://mcp.test/#x"],
)
def test_reject_unsafe_issuer_configuration(issuer):
    with pytest.raises(ValueError):
        OAuthFacade(
            issuer,
            GitHub("id", "secret"),
            resource="https://mcp.test/mcp",
            clients=[OAuthClient("console", ["https://mcp.test/console/oauth-callback"])],
        )
