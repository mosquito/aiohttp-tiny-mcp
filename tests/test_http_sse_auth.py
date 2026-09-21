"""Authorization on both halves of the legacy HTTP+SSE transport."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from urllib.parse import parse_qs, urlsplit

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from test_auth import RESOURCE, TOKENS, Nothing, build
from test_http_sse import OldClient

from aiohttp_tiny_mcp import Endpoint, Registry, SseEndpoint
from aiohttp_tiny_mcp.namespaces import current, namespace, scoped
from aiohttp_tiny_mcp.sessions import stored_owner

pytestmark = pytest.mark.asyncio

CALL = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "tools/call",
    "params": {"name": "public", "arguments": {}},
}


@asynccontextmanager
async def serving(registry, *, middleware=()):
    app = SseEndpoint(registry).setup(web.Application(middlewares=middleware))
    async with TestServer(app) as server:
        yield str(server.make_url(""))


@pytest.mark.parametrize("token", [None, "invalid", "stale", "unscoped"])
@pytest.mark.parametrize("method", ["GET", "POST"])
async def test_authentication_precedes_session_access(token, method):
    registry, _ = build(required_scopes=["reports:read"])
    async with serving(registry) as base:
        async with aiohttp.ClientSession() as http:
            headers = {"Authorization": f"Bearer {token}"} if token else {}
            path = "/sse" if method == "GET" else "/messages?session_id=unknown"
            async with http.request(method, base + path, headers=headers, json=CALL) as response:
                assert response.status == (403 if token == "unscoped" else 401)
                assert response.headers["WWW-Authenticate"].startswith("Bearer ")
                assert registry.auth.metadata_url in response.headers["WWW-Authenticate"]
            assert not registry.session_store.records


@pytest.mark.parametrize("token", [None, "invalid", "stale", "unscoped"])
async def test_each_post_rechecks_authorization_before_dispatch(token):
    registry, _ = build(required_scopes=["reports:read"])
    called = []

    @registry.tool
    async def probe(args: Nothing) -> str:
        called.append(True)
        return "called"

    async with serving(registry) as base:
        client = OldClient(base, headers={"Authorization": "Bearer alice"})
        try:
            await client.open()
            async with aiohttp.ClientSession() as http:
                headers = {"Authorization": f"Bearer {token}"} if token else {}
                payload = {**CALL, "params": {"name": "probe", "arguments": {}}}
                async with http.post(base + client.post_to, headers=headers, json=payload) as reply:
                    assert reply.status == (403 if token == "unscoped" else 401)
                    assert "WWW-Authenticate" in reply.headers
            assert called == []
            assert client.seen.empty()
        finally:
            await client.close()


@pytest.mark.parametrize("token", ["alice", "unscoped"])
async def test_legacy_handshake_principal_injection_and_tool_scopes(token):
    registry, _ = build()
    async with serving(registry) as base:
        client = OldClient(base, headers={"Authorization": f"Bearer {token}"})
        try:
            await client.open()
            initialized = await client.call(
                "initialize",
                {
                    "protocolVersion": "2024-11-05",
                    "clientInfo": {"name": "old", "version": "1"},
                    "capabilities": {},
                },
            )
            assert initialized["result"]["protocolVersion"] == "2024-11-05"
            await client.send("notifications/initialized", {}, request_id=None)
            result = await client.call("tools/call", {"name": "report", "arguments": {}}, 2)
            assert result["result"].get("isError", False) == (token == "unscoped")
            text = result["result"]["content"][0]["text"]
            assert ("reports:read" if token == "unscoped" else "report for alice") in text
        finally:
            await client.close()


@pytest.mark.parametrize("mode", ["token", "shared", "middleware"])
async def test_session_ownership_across_workers(mode):
    registry, _ = build(namespace_from_token=mode != "shared")

    @web.middleware
    async def tenant(request, handler):
        namespace.set("tenant")
        return await handler(request)

    @registry.tool
    async def location(args: Nothing) -> str:
        return current() or "unset"

    middleware = [tenant] if mode == "middleware" else []
    # Separate endpoints share only the registry's store, hub, and configuration.
    async with serving(registry, middleware=middleware) as first:
        async with serving(registry, middleware=middleware) as second:
            client = OldClient(first, headers={"Authorization": "Bearer alice"})
            try:
                await client.open()
                session_id = parse_qs(urlsplit(client.post_to).query)["session_id"][0]
                expected = {
                    "token": TOKENS["alice"].identity,
                    "shared": None,
                    "middleware": "tenant",
                }[mode]
                marker = namespace.set(expected)
                try:
                    record = await registry.session_store.get(scoped(session_id))
                finally:
                    namespace.reset(marker)
                assert record is not None
                assert stored_owner(record) == TOKENS["alice"].identity
                async with aiohttp.ClientSession() as http:
                    async with http.post(
                        second + client.post_to,
                        json=CALL,
                        headers={"Authorization": "Bearer bob"},
                    ) as refused:
                        assert refused.status == 404
                    assert client.seen.empty()
                    payload = {**CALL, "params": {"name": "location", "arguments": {}}}
                    async with http.post(
                        second + client.post_to,
                        json=payload,
                        headers={"Authorization": "Bearer alice"},
                    ) as accepted:
                        assert accepted.status == 202
                reply = await asyncio.wait_for(client.seen.get(), 5)
                assert reply["result"]["content"][0]["text"] == (expected or "unset")
            finally:
                await client.close()


@pytest.mark.parametrize("mount", ["root", "subapp", "both"])
async def test_metadata_routes(mount):
    registry, _ = build()
    legacy = SseEndpoint(registry)
    app = web.Application()
    if mount == "root":
        legacy.setup(app)
    elif mount == "subapp":
        section = web.Application()
        section.add_routes(legacy.routes(metadata=False))
        app.add_subapp("/legacy", section)
        app.add_routes(legacy.metadata_routes())
    else:
        Endpoint(registry).setup(app)
        legacy.setup(app, metadata=False)
    async with TestClient(TestServer(app)) as http:
        response = await http.get(registry.auth.metadata_path)
        assert response.status == 200
        assert (await response.json())["resource"] == RESOURCE
        path = "/legacy/sse" if mount == "subapp" else "/sse"
        refusal = await http.get(path)
        assert refusal.status == 401
        assert registry.auth.metadata_url in refusal.headers["WWW-Authenticate"]


async def test_no_metadata_without_auth():
    assert SseEndpoint(Registry("open", "1")).metadata_routes() == []
