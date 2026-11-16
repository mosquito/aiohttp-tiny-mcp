"""Resource notifications cannot inherit permissions from another token on the session."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from aiohttp.test_utils import TestClient, TestServer
from pydantic import BaseModel
from test_http_sse import OldClient
from test_subscriptions import LEGACY, VERSION, event, handshake, message

from aiohttp_tiny_mcp import Endpoint, MemoryHub, Registry, SseEndpoint
from aiohttp_tiny_mcp.auth import Authorization, Principal, StaticVerifier
from aiohttp_tiny_mcp.storage.hub import NOTIFICATIONS, topic
from aiohttp_tiny_mcp.storage.namespaces import namespace, scoped
from aiohttp_tiny_mcp.storage.sessions import SUBSCRIPTIONS_KEY

pytestmark = pytest.mark.timeout(10)
PRIVATE = ["files://private", "files://entries/42"]
PUBLIC = "files://public"


class Nothing(BaseModel):
    pass


class Entry(BaseModel):
    id: int


def headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def registry() -> Registry:
    tokens = {
        name: Principal(subject="same-user", namespace="tenant", scopes=frozenset(scopes))
        for name, scopes in (("weak", []), ("strong", ["files:read"]))
    }
    registry = Registry(
        "resources",
        "1",
        hub=MemoryHub(),
        hub_poll_seconds=0.01,
        auth=Authorization(
            verifier=StaticVerifier(tokens),
            resource="https://example.test/mcp",
            scopes_supported=["files:read"],
        ),
    )

    @registry.resource(PUBLIC)
    async def public(args: Nothing) -> str:
        return "public"

    @registry.resource(PRIVATE[0], scopes={"files:read"})
    async def private(args: Nothing) -> str:
        return "private"

    @registry.resource("files://entries/{id}", scopes={"files:read"})
    async def entry(args: Entry) -> str:
        return str(args.id)

    return registry


async def changed(registry: Registry, *uris: str) -> None:
    current = namespace.set("tenant")
    try:
        for uri in uris:
            await registry.hub.publish(
                topic(NOTIFICATIONS),
                {
                    "jsonrpc": "2.0",
                    "method": "notifications/resources/updated",
                    "params": {"uri": uri},
                },
            )
    finally:
        namespace.reset(current)


async def subscriptions(registry: Registry, session: str) -> dict[str, Any]:
    current = namespace.set("tenant")
    try:
        record = await registry.session_store.get(scoped(session))
        assert record is not None
        return dict(record.data.get(SUBSCRIPTIONS_KEY, {}).get("uris", {}))
    finally:
        namespace.reset(current)


async def legacy_call(
    client: TestClient, session: str, method: str, uri: str, token: str
) -> tuple[int, dict[str, Any]]:
    response = await client.post(
        "/mcp",
        headers={
            **headers(token),
            "Accept": "application/json",
            "MCP-Protocol-Version": LEGACY,
            "Mcp-Session-Id": session,
        },
        json={"jsonrpc": "2.0", "id": 2, "method": method, "params": {"uri": uri}},
    )
    return response.status, await response.json()


@pytest.mark.parametrize("token", ["weak", "strong"])
async def test_modern_listen_ack_filters_resource_scopes(registry: Registry, token: str):
    async with TestClient(TestServer(Endpoint(registry).app())) as client:
        payload = message(1, notifications={"resourceSubscriptions": [*PRIVATE, PUBLIC]})
        stream = await client.post(
            "/mcp",
            headers={
                **headers(token),
                "MCP-Protocol-Version": VERSION,
                "Mcp-Method": "subscriptions/listen",
                "Accept": "text/event-stream",
            },
            json=payload,
        )
        try:
            assert stream.status == 200
            ack = await event(stream)
            wanted = [*PRIVATE, PUBLIC] if token == "strong" else [PUBLIC]
            assert ack["params"]["notifications"]["resourceSubscriptions"] == wanted
            await changed(registry, *PRIVATE, PUBLIC)
            for uri in wanted:
                update = await event(stream)
                assert update["params"]["uri"] == uri
        finally:
            stream.close()


@pytest.mark.parametrize("uri", PRIVATE)
async def test_legacy_subscribe_denies_without_mutation_and_unsubscribe_is_allowed(
    registry: Registry, uri: str
):
    async with TestClient(TestServer(Endpoint(registry).app()), headers=headers("weak")) as client:
        session, _ = await handshake(client)
        status, denied = await legacy_call(client, session, "resources/subscribe", uri, "weak")
        assert status == 403
        assert denied["error"]["code"] == -32003
        assert await subscriptions(registry, session) == {}
        status, granted = await legacy_call(client, session, "resources/subscribe", uri, "strong")
        assert status == 200
        assert granted["result"] == {}
        assert await subscriptions(registry, session) == {uri: True}
        status, removed = await legacy_call(client, session, "resources/unsubscribe", uri, "weak")
        assert status == 200
        assert removed["result"] == {}
        assert await subscriptions(registry, session) == {}


@pytest.mark.parametrize("uri", PRIVATE)
async def test_http_get_stream_uses_its_own_scopes_after_stronger_token_subscribes(
    registry: Registry, uri: str
):
    async with TestClient(TestServer(Endpoint(registry).app()), headers=headers("weak")) as client:
        session, _ = await handshake(client)
        status, _ = await legacy_call(client, session, "resources/subscribe", PUBLIC, "weak")
        assert status == 200
        stream = await client.get(
            "/mcp",
            headers={
                "Accept": "text/event-stream",
                "MCP-Protocol-Version": LEGACY,
                "Mcp-Session-Id": session,
            },
        )
        try:
            assert stream.status == 200
            status, body = await legacy_call(client, session, "resources/subscribe", uri, "strong")
            assert status == 200 and body["result"] == {}
            await changed(registry, uri, PUBLIC)
            assert (await event(stream))["params"]["uri"] == PUBLIC
        finally:
            stream.close()


@pytest.mark.parametrize("uri", PRIVATE)
async def test_http_sse_stream_uses_its_own_scopes_after_stronger_token_subscribes(
    registry: Registry, uri: str
):
    app = Endpoint(registry).app()
    SseEndpoint(registry).setup(app)
    async with TestServer(app) as server:
        client = OldClient(str(server.make_url("")).rstrip("/"), headers=headers("weak"))
        try:
            await client.open()
            await client.call(
                "initialize",
                {
                    "protocolVersion": "2024-11-05",
                    "clientInfo": {"name": "scope-test", "version": "1"},
                    "capabilities": {},
                },
            )
            public = await client.call("resources/subscribe", {"uri": PUBLIC}, request_id=2)
            assert public["result"] == {}
            denied = await client.call("resources/subscribe", {"uri": uri}, request_id=3)
            assert denied["error"]["code"] == -32003
            async with client.http.post(
                f"{client.base}{client.post_to}",
                headers=headers("strong"),
                json={
                    "jsonrpc": "2.0",
                    "id": 4,
                    "method": "resources/subscribe",
                    "params": {"uri": uri},
                },
            ) as response:
                assert response.status == 202
            subscribed = await asyncio.wait_for(client.seen.get(), 2)
            assert subscribed["id"] == 4 and subscribed["result"] == {}
            await changed(registry, uri, PUBLIC)
            update = await asyncio.wait_for(client.seen.get(), 2)
            assert update["params"]["uri"] == PUBLIC
            removed = await client.call("resources/unsubscribe", {"uri": uri}, request_id=5)
            assert removed["result"] == {}
        finally:
            await client.close()
