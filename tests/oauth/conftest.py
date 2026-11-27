from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import os
import secrets
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest
from aiohttp import ClientSession, web
from aiohttp.test_utils import TestClient, TestServer
from yarl import URL

from aiohttp_tiny_mcp import Endpoint, Principal, Registry
from aiohttp_tiny_mcp.console import Console
from aiohttp_tiny_mcp.oauth import (
    EncryptedTokens,
    KECCAKCipher,
    OAuthClient,
    OAuthServer,
)
from aiohttp_tiny_mcp.oauth.github import GitHub, github_identity
from tests.oauth.helpers import ADAPTER, ISSUER, RESOURCE, Nothing


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


@pytest.fixture
async def authority():
    """A small independent AS with strict code/resource/PKCE binding and rotation."""
    state = SimpleNamespace(
        calls=[],
        codes={},
        refresh={},
        current=None,
        registered=[],
        saved=None,
        metadata_changes={},
        resource_changes={},
        token_changes={},
        token_error=False,
        reject=False,
        forbidden=False,
        redirect=False,
        return_refresh=True,
        challenge=True,
        challenge_scope="read",
        oidc=False,
        issuer_response=True,
    )
    app = web.Application()

    async def metadata(request):
        state.calls.append(("metadata", request.path, dict(request.headers)))
        if state.oidc and "oauth-authorization-server" in request.path:
            raise web.HTTPNotFound()
        return web.json_response(
            {
                "issuer": state.issuer,
                "authorization_endpoint": state.origin + "/authorize",
                "token_endpoint": state.origin + "/token",
                "registration_endpoint": state.origin + "/register",
                "response_types_supported": ["code"],
                "grant_types_supported": ["authorization_code", "refresh_token"],
                "code_challenge_methods_supported": ["S256"],
                "token_endpoint_auth_methods_supported": [
                    "none",
                    "client_secret_basic",
                    "client_secret_post",
                ],
                "authorization_response_iss_parameter_supported": True,
                "client_id_metadata_document_supported": True,
                **state.metadata_changes,
            }
        )

    async def resource_metadata(request):
        state.calls.append(("resource_metadata", request.path, dict(request.headers)))
        return web.json_response(
            {
                "resource": state.resource,
                "authorization_servers": [state.issuer],
                "scopes_supported": ["read"],
                **state.resource_changes,
            }
        )

    async def register(request):
        data = await request.json()
        state.registered.append(data)
        return web.json_response({**data, "client_id": "dynamic-client"}, status=201)

    async def authorize(request):
        data = dict(request.query)
        assert data["resource"] == state.resource
        assert data["code_challenge_method"] == "S256"
        code = secrets.token_urlsafe(24)
        state.codes[code] = data
        params = {"state": data["state"], "code": code}
        if state.issuer_response:
            params["iss"] = state.issuer
        raise web.HTTPFound(location=str(URL(data["redirect_uri"]).with_query(params)))

    async def token(request):
        form = dict(await request.post())
        state.calls.append(("token", form, dict(request.headers)))
        if state.token_error:
            return web.json_response(
                {"error": "invalid_grant", "error_description": "SECRET"}, status=400
            )
        assert form["resource"] == state.resource
        if form["grant_type"] == "authorization_code":
            saved = state.codes.pop(form["code"])
            assert saved["client_id"] == form["client_id"]
            assert saved["redirect_uri"] == form["redirect_uri"]
            digest = hashlib.sha256(form["code_verifier"].encode()).digest()
            assert base64.urlsafe_b64encode(digest).rstrip(b"=").decode() == saved["code_challenge"]
        else:
            if form["refresh_token"] not in state.refresh:
                return web.json_response({"error": "invalid_grant"}, status=400)
            state.refresh.pop(form["refresh_token"])
            # Let other callers reach the client's refresh lock.
            await asyncio.sleep(0.01)
        state.current = secrets.token_urlsafe(24)
        result = {"access_token": state.current, "token_type": "Bearer", "expires_in": 3600}
        if state.return_refresh:
            refresh = secrets.token_urlsafe(24)
            state.refresh[refresh] = True
            result["refresh_token"] = refresh
        return web.json_response({**result, **state.token_changes})

    async def mcp(request):
        state.calls.append(("mcp", request.method, dict(request.headers)))
        challenge = (
            {
                "WWW-Authenticate": (
                    f'Bearer resource_metadata="{state.origin}/metadata", '
                    f'scope="{state.challenge_scope}"'
                )
            }
            if state.challenge
            else {}
        )
        if request.headers.get("Authorization") != f"Bearer {state.current}" or state.reject:
            raise web.HTTPUnauthorized(headers=challenge)
        if state.forbidden:
            raise web.HTTPForbidden(
                headers={"WWW-Authenticate": 'Bearer error="insufficient_scope", scope="write"'}
            )
        if state.redirect:
            raise web.HTTPFound(location=state.origin + "/must-not-follow")
        if request.method == "GET":
            return web.Response(
                text='id: 1\ndata: {"jsonrpc":"2.0","method":"changed"}\n\n',
                content_type="text/event-stream",
            )
        envelope = await request.json()
        if "id" not in envelope or "method" not in envelope:
            return web.Response(status=202)
        method = envelope["method"]
        if method == "initialize":
            result = {
                "protocolVersion": ADAPTER.version,
                "serverInfo": {"name": "fake", "version": "1"},
                "capabilities": {},
            }
        elif method == "tools/list":
            result = {"tools": []}
        elif method == "tools/call":
            result = {"content": [{"type": "text", "text": "private result"}]}
        else:
            result = {}
        return web.json_response({"jsonrpc": "2.0", "id": envelope["id"], "result": result})

    app.router.add_get("/metadata", resource_metadata)
    app.router.add_get("/.well-known/oauth-protected-resource/mcp", resource_metadata)
    app.router.add_get("/.well-known/oauth-protected-resource", resource_metadata)
    app.router.add_get("/.well-known/oauth-authorization-server/tenant", metadata)
    app.router.add_get("/.well-known/openid-configuration/tenant", metadata)
    app.router.add_get("/tenant/.well-known/openid-configuration", metadata)
    app.router.add_post("/register", register)
    app.router.add_get("/authorize", authorize)
    app.router.add_post("/token", token)
    app.router.add_route("*", "/mcp", mcp)
    async with TestServer(app) as server:
        state.origin = str(server.make_url("/")).rstrip("/")
        state.resource = state.origin + "/mcp"
        state.issuer = state.origin + "/tenant"

        async def browser(url):
            async with ClientSession() as session:
                async with session.get(url, allow_redirects=False) as response:
                    assert response.status == 302
                    return response.headers["Location"]

        async def load():
            return copy.deepcopy(state.saved)

        async def save(record):
            state.saved = copy.deepcopy(record)

        state.browser, state.load, state.save = browser, load, save
        state.mcp_handler = mcp
        yield state


@pytest.fixture
def key():
    return os.urandom(32)


@pytest.fixture
def tokens(key):
    return EncryptedTokens(KECCAKCipher(key), issuer=ISSUER, resource=RESOURCE)


@pytest.fixture
def principal():
    return Principal(
        subject="alice",
        issuer=ISSUER,
        client_id="console",
        scopes=frozenset({"read", "write"}),
        expires_at=time.time() + 300,
        namespace="team",
        claims={"display_name": "Alice"},
    )
