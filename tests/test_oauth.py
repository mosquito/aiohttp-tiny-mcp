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
from aiohttp_tiny_mcp.oauth import (
    Identity,
    OAuthClient,
    OAuthProvider,
    OAuthServer,
    UpstreamAuthError,
)
from aiohttp_tiny_mcp.oauth.github import GitHub, github_identity
from aiohttp_tiny_mcp.oauth.server import challenge

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
    server = OAuthServer(
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
    registry = Registry("private", "1", auth=server.resource(required_scopes=["read"]))

    @registry.tool()
    async def who(args: Nothing, principal: Principal) -> str:
        return principal.subject or ""

    app = Endpoint(registry).app()
    server.setup(app)
    Console(oauth_client_id="console").setup(app)
    async with TestClient(TestServer(app, port=unused_tcp_port)) as client:
        yield server, client


def parameters(server, **changes):
    return {
        "client_id": "console",
        "redirect_uri": server.origin + "/console/oauth-callback",
        "response_type": "code",
        "resource": server.resource_url,
        "code_challenge": challenge(VERIFIER),
        "code_challenge_method": "S256",
        "state": "client-state",
        "scope": "read",
        **changes,
    }


async def consent(server, client, **changes):
    response = await client.get("/oauth/authorize", params=parameters(server, **changes))
    assert response.status == 200, await response.text()
    text = await response.text()
    assert "Console" in text and "read" in text
    assert response.headers["Cache-Control"] == "no-store"
    # Chrome serializes the form's Origin as null with no-referrer.
    assert response.headers["Referrer-Policy"] == "same-origin"
    assert (
        str(URL(server.provider.authorize_url).origin())
        in response.headers["Content-Security-Policy"]
    )
    cookie = next(iter(response.cookies.values()))
    assert cookie["httponly"] and cookie["samesite"] == "Lax"
    return re.search(r'name="ticket" value="([^"]+)"', text)[1]


async def upstream_redirect(server, client):
    ticket = await consent(server, client)
    response = await client.post(
        "/oauth/authorize",
        data={"ticket": ticket, "decision": "allow"},
        headers={"Origin": server.origin},
        allow_redirects=False,
    )
    assert response.status == 303, await response.text()
    target = URL(response.headers["Location"])
    assert target.query["redirect_uri"] == server.callback_url
    assert target.query["code_challenge_method"] == "S256"
    assert target.query["state"] != "client-state"
    assert target.query["code_challenge"] != challenge(VERIFIER)
    return target


async def issued_code(server, client):
    target = await upstream_redirect(server, client)
    response = await client.get(
        "/oauth/callback",
        params={"state": target.query["state"], "code": "provider-code"},
        allow_redirects=False,
    )
    assert response.status == 303, await response.text()
    callback = URL(response.headers["Location"])
    assert callback.query["iss"] == server.issuer
    assert callback.query["state"] == "client-state"
    assert "github-secret" not in response.headers["Location"]
    return callback.query["code"], target


def token_params(server, code, **changes):
    return {
        "grant_type": "authorization_code",
        "client_id": "console",
        "code": code,
        "code_verifier": VERIFIER,
        "redirect_uri": server.origin + "/console/oauth-callback",
        "resource": server.resource_url,
        **changes,
    }


async def access_token(server, client):
    code, _ = await issued_code(server, client)
    response = await client.post("/oauth/token", data=token_params(server, code))
    assert response.status == 200, await response.text()
    return await response.json()


async def test_registered_client_redirects_directly_by_default(protected, provider):
    server, client = protected
    server.clients["console"] = OAuthClient("console", [server.origin + "/console/oauth-callback"])
    response = await client.get(
        "/oauth/authorize", params=parameters(server), allow_redirects=False
    )
    assert response.status == 303
    assert response.headers["Cache-Control"] == "no-store"
    target = URL(response.headers["Location"])
    assert target.with_query(None) == URL(server.provider.authorize_url)
    assert target.query["redirect_uri"] == server.callback_url
    assert target.query["code_challenge_method"] == "S256"
    assert target.query["code_challenge"] != challenge(VERIFIER)
    assert target.query["state"] != "client-state"
    cookie = next(iter(response.cookies.values()))
    assert cookie["httponly"] and cookie["samesite"] == "Lax"
    assert not any("/consent/" in key for key in server.store.records)

    callback_params = {"state": target.query["state"], "code": "provider-code"}
    async with ClientSession() as stranger:
        denied = await stranger.get(server.callback_url, params=callback_params)
        assert denied.status == 400
        assert (await denied.json())["error"] == "invalid_request"
    assert not provider[1]

    response = await client.get("/oauth/callback", params=callback_params, allow_redirects=False)
    assert response.status == 303
    callback = URL(response.headers["Location"])
    assert callback.query["state"] == "client-state"
    assert callback.query["iss"] == server.issuer
    response = await client.post("/oauth/token", data=token_params(server, callback.query["code"]))
    assert response.status == 200
    result = await response.json()
    principal = await server.verify(result["access_token"])
    assert principal.subject == "github:123"
    assert principal.scopes == {"read"}


@pytest.mark.parametrize("encrypted", [False, True])
async def test_github_sign_in_produces_resource_bound_mcp_token(protected, provider, encrypted):
    server, client = protected
    if encrypted:
        import os

        from aiohttp_tiny_mcp.oauth import EncryptedTokens, KECCAKCipher

        server.tokens = EncryptedTokens(
            KECCAKCipher(os.urandom(32)), issuer=server.issuer, resource=server.resource_url
        )
    metadata = await (await client.get("/.well-known/oauth-authorization-server/oauth")).json()
    assert metadata["issuer"] == server.issuer
    assert metadata["code_challenge_methods_supported"] == ["S256"]
    assert metadata["grant_types_supported"] == ["authorization_code"]
    prm = await (await client.get("/.well-known/oauth-protected-resource/mcp")).json()
    assert prm["authorization_servers"] == [server.issuer]
    result = await access_token(server, client)
    token = result["access_token"]
    assert token != "github-secret"
    assert result["token_type"] == "Bearer" and 0 < result["expires_in"] <= 3600
    assert "refresh_token" not in result
    principal = await server.verify(token)
    assert principal.subject == "github:123"
    assert principal.issuer == server.issuer
    assert principal.client_id == "console"
    assert principal.scopes == {"read"}
    assert principal.claims["login"] == "alice"
    assert await server.verify("github-secret") is None
    assert "github-secret" not in repr(server.store.records)
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
    if encrypted:
        with pytest.raises(NotImplementedError):
            await server.revoke(token)
    else:
        await server.revoke(token)
        assert await server.verify(token) is None


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
    server, client = protected
    response = await client.get(
        "/oauth/authorize", params=parameters(server, **changes), allow_redirects=False
    )
    if response.status == 303:
        url = URL(response.headers["Location"])
        assert url.origin() == URL(server.origin)
        assert url.query["error"] == error
        assert url.query["iss"] == server.issuer
    else:
        assert response.status == 400
        assert (await response.json())["error"] == error
        assert "Location" not in response.headers


async def test_browser_binding_origin_and_consent_replay(protected, provider):
    server, client = protected
    ticket = await consent(server, client)
    data = {"ticket": ticket, "decision": "allow"}
    wrong = await client.post(
        "/oauth/authorize", data=data, headers={"Origin": "https://evil.test"}
    )
    assert wrong.status == 403
    async with ClientSession() as stranger:
        response = await stranger.post(
            server.issuer + "/authorize", data=data, headers={"Origin": server.origin}
        )
        assert response.status == 400
    response = await client.post(
        "/oauth/authorize", data=data, headers={"Origin": server.origin}, allow_redirects=False
    )
    assert response.status == 303
    target = URL(response.headers["Location"])
    repeated = await client.post(
        "/oauth/authorize", data=data, headers={"Origin": server.origin}, allow_redirects=False
    )
    assert repeated.status == 400
    query = {"state": target.query["state"], "code": "provider-code"}
    async with ClientSession() as stranger:
        response = await stranger.get(server.callback_url, params=query, allow_redirects=False)
        assert response.status == 400
    assert not provider[1]
    first = await client.get("/oauth/callback", params=query, allow_redirects=False)
    assert first.status == 303
    repeated = await client.get("/oauth/callback", params=query, allow_redirects=False)
    assert repeated.status == 400
    assert len(provider[1]) == 1
    assert challenge(provider[1][0]["code_verifier"]) == target.query["code_challenge"]


async def test_denied_consent_never_contacts_provider(protected, provider):
    server, client = protected
    ticket = await consent(server, client)
    response = await client.post(
        "/oauth/authorize",
        data={"ticket": ticket, "decision": "deny"},
        headers={"Origin": server.origin},
        allow_redirects=False,
    )
    query = URL(response.headers["Location"]).query
    assert query["error"] == "access_denied" and query["iss"] == server.issuer
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
    server, client = protected
    code, _ = await issued_code(server, client)
    response = await client.post("/oauth/token", data=token_params(server, code, **changes))
    assert response.status == 400
    valid = await client.post("/oauth/token", data=token_params(server, code))
    assert valid.status == 200
    repeated = await client.post("/oauth/token", data=token_params(server, code))
    assert repeated.status == 400


async def test_only_one_concurrent_code_exchange_succeeds(protected):
    server, client = protected
    code, _ = await issued_code(server, client)
    responses = await asyncio.gather(
        *(client.post("/oauth/token", data=token_params(server, code)) for _ in range(8))
    )
    assert sorted(response.status for response in responses) == [200] + [400] * 7


async def test_shared_store_accepts_tokens_on_another_worker(protected, provider):
    server, client = protected
    result = await access_token(server, client)
    worker = OAuthServer(
        server.issuer,
        provider[0],
        resource=server.resource_url,
        clients=list(server.clients.values()),
        scopes=server.scopes,
        store=server.store,
    )
    assert (await worker.verify(result["access_token"])).subject == "github:123"
    other = OAuthServer(
        server.issuer,
        provider[0],
        resource=server.origin + "/other",
        clients=list(server.clients.values()),
        store=server.store,
    )
    assert await other.verify(result["access_token"]) is None


async def test_expired_tokens_and_flows_are_rejected(protected, monkeypatch):
    server, client = protected
    token = (await access_token(server, client))["access_token"]
    ticket = await consent(server, client)
    now = time.time()
    monkeypatch.setattr("aiohttp_tiny_mcp.oauth.server.time.time", lambda: now + 7200)
    assert await server.verify(token) is None
    response = await client.post(
        "/oauth/authorize",
        data={"ticket": ticket, "decision": "allow"},
        headers={"Origin": server.origin},
        allow_redirects=False,
    )
    assert response.status == 400


async def test_custom_identity_mapper_controls_access_and_namespace(protected):
    server, client = protected

    async def mapper(identity):
        assert identity == Identity(
            "123", "github", {"login": "alice", "name": "Alice", "email": None}
        )
        return Principal(subject="internal-user", namespace="team", scopes=frozenset({"read"}))

    server.identity = mapper
    principal = await server.verify((await access_token(server, client))["access_token"])
    assert principal.subject == "internal-user" and principal.namespace == "team"
    server.identity = lambda identity: None
    target = await upstream_redirect(server, client)
    response = await client.get(
        "/oauth/callback",
        params={"state": target.query["state"], "code": "provider-code"},
        allow_redirects=False,
    )
    assert URL(response.headers["Location"]).query["error"] == "access_denied"


@pytest.mark.parametrize("bad", [True, None, 0, "123"])
async def test_github_rejects_invalid_stable_user_id(protected, provider, bad):
    server, client = protected
    provider[3]["id"] = bad
    target = await upstream_redirect(server, client)
    response = await client.get(
        "/oauth/callback",
        params={"state": target.query["state"], "code": "provider-code"},
        allow_redirects=False,
    )
    assert URL(response.headers["Location"]).query["error"] == "temporarily_unavailable"


async def test_optional_github_expiry_and_refresh_token_are_not_mcp_credentials(provider):
    upstream, _, data, _ = provider
    data.update(
        expires_in=28800,
        refresh_token="github-refresh-secret",
        provider_extension={"account": "alice", "flags": [1, 2]},
    )
    async with ClientSession() as http:
        tokens = await upstream.exchange(
            http,
            code="provider-code",
            redirect_uri="https://mcp.test/oauth/callback",
            verifier=VERIFIER,
        )
    assert type(tokens) is dict
    assert tokens == data
    assert tokens["refresh_token"] == "github-refresh-secret"
    assert tokens["expires_in"] == 28800
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
    server = OAuthServer(
        origin + "/oauth",
        provider[0],
        resource=origin + "/mcp",
        clients=[OAuthClient("console", [origin + "/console/oauth-callback"])],
    )

    @web.middleware
    async def protect(request, handler):
        if Console.is_public(request) or OAuthServer.is_public(request):
            return await handler(request)
        raise web.HTTPUnauthorized()

    app = web.Application(middlewares=[protect])
    server.setup(app)
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
        OAuthServer(
            issuer,
            GitHub("id", "secret"),
            resource="https://mcp.test/mcp",
            clients=[OAuthClient("console", ["https://mcp.test/console/oauth-callback"])],
        )


class ApplicationTokens:
    """An application can store credentials without inheriting a library type."""

    def __init__(self):
        self.grants = {}

    async def issue(self, principal, upstream):
        token = f"application-token-{len(self.grants)}"
        self.grants[token] = (principal, dict(upstream))
        return token

    async def verify(self, token):
        grant = self.grants.get(token)
        return grant[0] if grant else None

    async def revoke(self, token):
        self.grants.pop(token, None)


def another_worker(server, provider, **changes):
    return OAuthServer(
        server.issuer,
        provider[0],
        resource=server.resource_url,
        clients=list(server.clients.values()),
        scopes=server.scopes,
        **{"store": server.store, "tokens": server.tokens, **changes},
    )


async def test_application_owns_token_issuance_and_storage(protected, provider):
    server, client = protected
    server.tokens = tokens = ApplicationTokens()
    provider[2]["provider_extension"] = {"refresh_hint": "opaque-provider-value"}
    first = (await access_token(server, client))["access_token"]
    provider[3].update(id=456, login="bob")
    second = (await access_token(server, client))["access_token"]
    assert first != second
    assert tokens.grants[first][0].subject == "github:123"
    assert tokens.grants[second][0].subject == "github:456"
    assert tokens.grants[first][1] == provider[2]
    assert "github-secret" not in repr(tokens.grants[first][0])
    assert "github-secret" not in repr(server.store.records)
    assert not any("/access/" in key for key in server.store.records)
    assert (await another_worker(server, provider).verify(first)).subject == "github:123"
    await server.revoke(first)
    assert await server.verify(first) is None
    assert (await server.verify(second)).subject == "github:456"


async def test_issue_runs_only_after_account_and_scope_checks(protected, provider):
    server, client = protected
    server.tokens = tokens = ApplicationTokens()
    server.identity = lambda identity: Principal(subject=identity.sub, scopes=frozenset())
    target = await upstream_redirect(server, client)
    response = await client.get(
        "/oauth/callback",
        params={"state": target.query["state"], "code": "provider-code"},
        allow_redirects=False,
    )
    assert URL(response.headers["Location"]).query["error"] == "access_denied"
    assert not tokens.grants


async def test_issue_runs_once_after_pkce_and_code_redemption(protected, provider):
    server, client = protected
    calls = []

    class Tokens(ApplicationTokens):
        async def issue(self, principal, upstream):
            assert await server.store.get(server.key("code", code)) is None
            calls.append((principal, upstream))
            await asyncio.sleep(0)
            return await super().issue(principal, upstream)

    server.tokens = Tokens()
    code, _ = await issued_code(server, client)
    assert not calls
    pending = await server.store.get(server.key("code", code))
    assert pending.data["upstream"] == provider[2]
    wrong = await client.post(
        "/oauth/token", data=token_params(server, code, code_verifier="wrong")
    )
    assert wrong.status == 400
    assert not calls
    results = await asyncio.gather(
        *(client.post("/oauth/token", data=token_params(server, code)) for _ in range(4))
    )
    assert sorted(result.status for result in results) == [200, 400, 400, 400]
    assert len(calls) == 1
    assert calls[0][1] == provider[2]


async def test_abandoned_code_expires_without_issuing_a_token(protected, monkeypatch):
    server, client = protected
    server.tokens = tokens = ApplicationTokens()
    code, _ = await issued_code(server, client)
    assert not tokens.grants
    now = time.time()
    monkeypatch.setattr("aiohttp_tiny_mcp.oauth.server.time.time", lambda: now + 301)
    response = await client.post("/oauth/token", data=token_params(server, code))
    assert response.status == 400
    assert not tokens.grants


async def test_custom_provider_exchange_needs_no_client_secret(protected, provider):
    server, client = protected
    original, calls, response, _ = provider

    class CustomProvider(OAuthProvider):
        async def exchange(self, http, *, code, redirect_uri, verifier):
            assert not self.client_secret
            assert code == "provider-code"
            assert redirect_uri == server.callback_url
            assert len(verifier) == 43
            return dict(response)

    custom = CustomProvider(
        name=original.name,
        authorize_url=original.authorize_url,
        token_url=original.token_url,
        client_id=original.client_id,
        scopes=original.scopes,
        profile=original.profile,
    )
    assert another_worker(server, (custom,)).provider is custom
    server.provider = custom
    token = (await access_token(server, client))["access_token"]
    assert (await server.verify(token)).subject == "github:123"
    assert not calls


async def test_default_provider_exchange_omits_absent_secret(provider):
    async def token(request):
        form = await request.post()
        assert "client_secret" not in form
        assert form["client_id"] == "public-client"
        assert form["code_verifier"] == VERIFIER
        return web.json_response({"access_token": "provider-token", "token_type": "bearer"})

    app = web.Application()
    app.router.add_post("/token", token)
    async with TestServer(app) as http_server, ClientSession() as http:
        public = replace(
            provider[0],
            client_id="public-client",
            client_secret="",
            token_url=str(http_server.make_url("/token")),
        )
        result = await public.exchange(
            http, code="code", redirect_uri="https://mcp.test/callback", verifier=VERIFIER
        )
    assert result["access_token"] == "provider-token"


def test_github_owns_its_secret_requirement():
    with pytest.raises(ValueError, match="GitHub client secret"):
        GitHub("client", "")


@pytest.mark.parametrize("result", [None, {}, "", "token\nsecret", "with spaces"])
async def test_invalid_custom_token_is_not_issued(protected, result):
    server, client = protected

    class InvalidTokens(ApplicationTokens):
        async def issue(self, principal, upstream):
            return result

    server.tokens = InvalidTokens()
    code, _ = await issued_code(server, client)
    response = await client.post("/oauth/token", data=token_params(server, code))
    assert response.status == 500
    assert (await response.json())["error"] == "server_error"
    assert "secret" not in await response.text()
    retry = await client.post("/oauth/token", data=token_params(server, code))
    assert retry.status == 400


async def test_custom_issuer_errors_do_not_expose_tokens(protected, caplog):
    server, client = protected

    class BrokenTokens(ApplicationTokens):
        async def issue(self, principal, upstream):
            raise RuntimeError(upstream)

    server.tokens = BrokenTokens()
    code, _ = await issued_code(server, client)
    response = await client.post("/oauth/token", data=token_params(server, code))
    assert response.status == 500
    assert (await response.json())["error"] == "server_error"
    assert "github-secret" not in await response.text() + caplog.text


async def test_custom_verifier_cannot_return_expired_or_wrong_issuer(protected):
    server, client = protected
    server.tokens = tokens = ApplicationTokens()
    token = (await access_token(server, client))["access_token"]
    principal, upstream = tokens.grants[token]
    for invalid in (
        replace(principal, expires_at=time.time() - 1),
        replace(principal, issuer="https://other.example"),
        {"sub": "not-a-principal"},
    ):
        tokens.grants[token] = (invalid, upstream)
        assert await server.verify(token) is None


async def test_revoke_is_optional_for_application_tokens(protected):
    server, _ = protected

    class StatelessTokens:
        async def issue(self, principal, upstream):
            return "unused"

        async def verify(self, token):
            return None

    server.tokens = StatelessTokens()
    with pytest.raises(NotImplementedError, match="does not support revoke"):
        await server.revoke("unused")


@pytest.mark.parametrize("tokens", [{}, object(), type("IssueOnly", (), {"issue": lambda: None})()])
def test_custom_tokens_require_issue_and_verify(tokens):
    with pytest.raises(TypeError, match="issue.*verify"):
        OAuthServer(
            "https://mcp.test/oauth",
            GitHub("id", "secret"),
            resource="https://mcp.test/mcp",
            clients=[OAuthClient("console", ["https://mcp.test/callback"])],
            tokens=tokens,
        )


@pytest.mark.parametrize("backend", ["application", "builtin"])
async def test_encrypted_tokens_and_code_redemption_across_workers(
    protected, provider, tmp_path, backend
):
    import json
    import secrets
    from base64 import b64decode, b64encode
    from contextlib import AsyncExitStack
    from dataclasses import asdict

    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    from aiohttp_tiny_mcp.storage.sqlite import SqliteSessionStore, SqliteStorage

    # A user-defined AES codec and the built-in codec must obey the same contract.
    class EncryptedTokens:
        def __init__(self, key, resource):
            self.cipher = AESGCM(key)
            self.resource = resource.encode()

        async def issue(self, principal, upstream):
            identity = asdict(principal)
            identity["scopes"] = sorted(principal.scopes)
            payload = json.dumps({"principal": identity, "upstream": upstream}).encode()
            nonce = secrets.token_bytes(12)
            return b64encode(nonce + self.cipher.encrypt(nonce, payload, self.resource)).decode()

        def decode(self, token):
            payload = b64decode(token, validate=True)
            return json.loads(self.cipher.decrypt(payload[:12], payload[12:], self.resource))

        async def verify(self, token):
            try:
                data = self.decode(token)["principal"]
                data["scopes"] = frozenset(data["scopes"])
                principal = Principal(**data)
                return None if principal.expired else principal
            except (InvalidTag, ValueError, KeyError, TypeError):
                return None

    server, client = protected

    def make_tokens(key, resource):
        if backend == "application":
            return EncryptedTokens(key, resource)
        from aiohttp_tiny_mcp.oauth import EncryptedTokens as BuiltinTokens
        from aiohttp_tiny_mcp.oauth import KECCAKCipher

        return BuiltinTokens(KECCAKCipher(key), issuer=server.issuer, resource=resource)

    key = AESGCM.generate_key(bit_length=256)
    path = str(tmp_path / "oauth.sqlite")
    async with AsyncExitStack() as stack:
        stores = [SqliteStorage(path) for _ in range(3)]
        for store in stores:
            stack.push_async_callback(store.close)
        server.store = SqliteSessionStore(stores[0])
        server.tokens = make_tokens(key, server.resource_url)
        workers = [server]
        clients = [client]
        for store in stores[1:]:
            worker = another_worker(
                server,
                provider,
                store=SqliteSessionStore(store),
                tokens=make_tokens(key, server.resource_url),
            )
            app = worker.setup(web.Application())
            other = await stack.enter_async_context(TestClient(TestServer(app)))
            workers.append(worker)
            clients.append(other)
        target = await upstream_redirect(server, client)
        # Simulate a reverse proxy routing the same browser to another worker.
        clients[1].session.cookie_jar.update_cookies(
            client.session.cookie_jar.filter_cookies(URL(server.issuer))
        )
        response = await clients[1].get(
            "/oauth/callback",
            params={"state": target.query["state"], "code": "provider-code"},
            allow_redirects=False,
        )
        code = URL(response.headers["Location"]).query["code"]
        wrong = await clients[2].post(
            "/oauth/token", data=token_params(server, code, code_verifier="x" * 43)
        )
        assert wrong.status == 400
        responses = await asyncio.gather(
            *(other.post("/oauth/token", data=token_params(server, code)) for other in clients[1:])
        )
        assert sorted(response.status for response in responses) == [200, 400]
        result = await next(response for response in responses if response.status == 200).json()
        token = result["access_token"]
        assert "github-secret" not in token
        # A worker needs only the shared key to verify and recover provider credentials.
        assert workers[2].tokens.decode(token)["upstream"] == provider[2]
        assert (await workers[2].verify(token)).subject == "github:123"
        assert await workers[2].verify(token[:-4] + "AAAA") is None
        assert await make_tokens(key, "other-resource").verify(token) is None
        assert (
            await make_tokens(AESGCM.generate_key(bit_length=256), server.resource_url).verify(
                token
            )
            is None
        )
        connection = await stores[0].open()
        async with connection.execute("SELECT data FROM mcp_sessions") as cursor:
            rows = await cursor.fetchall()
        assert not rows  # Redemption removes transient state; no access-token records exist.
        # A newly started worker with no shared state can still verify this token.
        fresh = another_worker(
            server, provider, store=None, tokens=make_tokens(key, server.resource_url)
        )
        assert (await fresh.verify(token)).subject == "github:123"
