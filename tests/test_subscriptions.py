from __future__ import annotations

import asyncio
import json

import pytest
from aiohttp.test_utils import TestClient, TestServer
from pydantic import BaseModel

from aiohttp_tiny_mcp import Endpoint, Exchange
from aiohttp_tiny_mcp.server.stdio import serve_stdio
from aiohttp_tiny_mcp.server.subscriptions import SUBSCRIPTION_ID
from aiohttp_tiny_mcp.storage.hub import Event
from aiohttp_tiny_mcp.storage.hub import Hub as HubProtocol

pytestmark = pytest.mark.asyncio
VERSION = "2026-07-28"


class Nothing(BaseModel):
    pass


class Hub(HubProtocol):
    """Cursor-based test hub with synchronous publish to exercise events arriving before the first
    poll.
    """

    def __init__(self):
        self.rows: dict[str, list[tuple[int, dict]]] = {}
        self.last_id = 0
        self.arrived = asyncio.Event()
        self.polling = 0
        self.positioned: list[str] = []

    def publish(self, method, topic=None, **params):
        self.last_id += 1
        name = topic if topic is not None else "notifications"
        row = {"jsonrpc": "2.0", "method": method, "params": params}
        self.rows.setdefault(name, []).append((self.last_id, row))
        self.arrived.set()

    async def position(self, topic):
        self.positioned.append(topic)
        rows = self.rows.get(topic)
        return str(rows[-1][0]) if rows else ""

    async def poll(self, topic, cursor, *, timeout):
        self.polling += 1
        try:
            return await self.look(topic, cursor, timeout)
        finally:
            self.polling -= 1

    async def look(self, topic, cursor, timeout):
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        least = int(cursor) if cursor else 0
        while True:
            found = [Event(str(n), message) for n, message in self.rows.get(topic, []) if n > least]
            if found:
                return found
            left = deadline - loop.time()
            if left <= 0:
                return []
            self.arrived.clear()
            try:
                await asyncio.wait_for(self.arrived.wait(), left)
            except asyncio.TimeoutError:
                return []

    async def delete(self, topic):
        self.rows.pop(topic, None)


def message(id, method="subscriptions/listen", **params):
    return {
        "jsonrpc": "2.0",
        "id": id,
        "method": method,
        "params": {
            **params,
            "_meta": {
                "io.modelcontextprotocol/protocolVersion": VERSION,
                "io.modelcontextprotocol/clientCapabilities": {},
            },
        },
    }


def feed(reader, payload):
    reader.feed_data((json.dumps(payload) + "\n").encode())


async def event(response):
    while line := await asyncio.wait_for(response.content.readline(), 2):
        if line.startswith(b"data:"):
            return json.loads(line[5:])
    pytest.fail("stream closed before event")


async def post(client, payload, accept="text/event-stream"):
    return await client.post(
        "/mcp",
        json=payload,
        headers={
            "MCP-Protocol-Version": VERSION,
            "Mcp-Method": payload["method"],
            "Accept": accept,
        },
    )


async def test_http_filter_and_ack(registry):
    registry.hub = hub = Hub()
    async with TestClient(TestServer(Endpoint(registry).app())) as client:
        response = await post(
            client,
            message(
                0,
                notifications={
                    "toolsListChanged": True,
                    "resourceSubscriptions": ["config://app", "unknown://resource"],
                },
            ),
        )
        ack = await event(response)
        assert ack["method"] == "notifications/subscriptions/acknowledged"
        assert ack["params"]["notifications"] == {
            "toolsListChanged": True,
            "resourceSubscriptions": ["config://app"],
        }
        assert ack["params"]["_meta"][SUBSCRIPTION_ID] == 0
        assert hub.positioned  # cursor taken before the ACK went out
        assert "Mcp-Session-Id" not in response.headers
        hub.publish("notifications/prompts/list_changed")
        hub.publish("notifications/progress", progress=1)
        hub.publish("notifications/resources/updated", uri="unknown://resource")
        hub.publish("notifications/resources/updated", uri="config://app", _meta={"custom": 1})
        update = await event(response)
        assert update["method"] == "notifications/resources/updated"
        assert update["params"]["_meta"] == {"custom": 1, SUBSCRIPTION_ID: 0}
        response.close()

        async def released():
            while hub.polling:
                await asyncio.sleep(0.01)

        await asyncio.wait_for(released(), 2)


async def test_listen_requires_sse_accept(client):
    response = await post(client, message(1), accept="application/json")
    assert response.status == 406


@pytest.mark.parametrize(
    "notifications",
    [
        {"toolsListChanged": "true"},
        {"resourceSubscriptions": [42]},
        [],
    ],
)
async def test_invalid_filter_does_not_open_stream(client, notifications):
    response = await post(
        client,
        message(1, notifications=notifications),
        accept="application/json, text/event-stream",
    )
    assert (await response.json())["error"]["code"] == -32602


async def test_every_revision_can_deliver_a_change(registry):
    """Modern request streams and legacy GET streams advertise the same capabilities."""
    from aiohttp_tiny_mcp.protocol.v2025_03_26 import Adapter2025_03_26
    from aiohttp_tiny_mcp.protocol.v2025_11_25 import Adapter2025_11_25
    from aiohttp_tiny_mcp.protocol.v2026_07_28 import Adapter2026_07_28

    for adapter in (Adapter2026_07_28(), Adapter2025_11_25(), Adapter2025_03_26()):
        caps = adapter.capabilities(registry)
        assert caps["tools"]["listChanged"] is True, adapter.version
        assert caps["resources"]["listChanged"] is True, adapter.version
        assert caps["resources"]["subscribe"] is True, adapter.version


@pytest.mark.parametrize("handler_cancellation", [False, True])
async def test_disconnect_releases_listener(registry, handler_cancellation):
    registry.hub = hub = Hub()

    class Server(TestServer):
        async def _make_runner(self, **kwargs):
            kwargs["handler_cancellation"] = handler_cancellation
            return await super()._make_runner(**kwargs)

    async with TestClient(Server(Endpoint(registry).app())) as client:
        response = await post(client, message(1, notifications={"toolsListChanged": True}))
        await event(response)
        response.close()

        async def released():
            while hub.polling:
                await asyncio.sleep(0.01)

        await asyncio.wait_for(released(), 2)


async def test_stdio_concurrent_subscriptions_cancel_and_eof(registry):
    registry.hub = hub = Hub()
    reader = asyncio.StreamReader()
    output = asyncio.Queue()
    server = asyncio.create_task(serve_stdio(registry, reader, output.put_nowait))

    async def receive():
        return await asyncio.wait_for(output.get(), 2)

    try:
        feed(reader, message(0, notifications={"toolsListChanged": True}))
        feed(
            reader, message("resources", notifications={"resourceSubscriptions": ["config://app"]})
        )
        assert (await receive())["params"]["_meta"][SUBSCRIPTION_ID] == 0
        assert (await receive())["params"]["_meta"][SUBSCRIPTION_ID] == "resources"
        feed(reader, message(2, "tools/call", name="add", arguments={"a": 2, "b": 3}))
        assert (await receive())["result"]["structuredContent"] == {"result": 5}
        feed(
            reader,
            {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": False}},
        )
        feed(reader, message(3, "tools/list"))
        assert (await receive())["id"] == 3
        assert hub.polling == 2
        feed(
            reader,
            {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 0}},
        )
        feed(reader, message(4, "tools/list"))
        assert (await receive())["id"] == 4
        assert hub.polling == 1
        hub.publish("notifications/tools/list_changed")
        hub.publish("notifications/resources/updated", uri="config://app")
        update = await receive()
        assert update["params"]["_meta"][SUBSCRIPTION_ID] == "resources"
        assert output.empty()
    finally:
        reader.feed_eof()
        await asyncio.wait_for(server, 2)
    assert hub.polling == 0


async def test_stdio_cancels_running_tool_without_result(registry):
    started, stopped = asyncio.Event(), asyncio.Event()

    @registry.tool
    async def wait(args: Nothing, ex: Exchange) -> str:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()
        return "unreachable"

    reader = asyncio.StreamReader()
    output = []
    server = asyncio.create_task(serve_stdio(registry, reader, output.append))
    try:
        feed(reader, message(1, "tools/call", name="wait"))
        await asyncio.wait_for(started.wait(), 2)
        feed(
            reader,
            {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 1}},
        )
        await asyncio.wait_for(stopped.wait(), 2)
    finally:
        reader.feed_eof()
        await asyncio.wait_for(server, 2)
    assert output == []


async def test_stdio_reselects_modern_version_for_each_request(registry):
    reader = asyncio.StreamReader()
    output = []
    feed(reader, message(1, "tools/list"))
    invalid = message(2, "tools/list")
    invalid["params"]["_meta"]["io.modelcontextprotocol/protocolVersion"] = "2099-01-01"
    feed(reader, invalid)
    feed(reader, message(3, "tools/list"))
    reader.feed_eof()
    await serve_stdio(registry, reader, output.append)
    assert output[0]["id"] == 1 and "result" in output[0]
    assert output[1]["error"]["code"] == -32022
    assert output[2]["id"] == 3 and "result" in output[2]


LEGACY = "2025-11-25"


async def legacy_post(client, payload, session=None):
    headers = {"MCP-Protocol-Version": LEGACY, "Accept": "application/json"}
    if session:
        headers["Mcp-Session-Id"] = session
    return await client.post("/mcp", json=payload, headers=headers)


async def handshake(client):
    response = await legacy_post(
        client,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": LEGACY,
                "clientInfo": {"name": "t", "version": "1"},
                "capabilities": {},
            },
        },
    )
    body = await response.json()
    return response.headers["Mcp-Session-Id"], body["result"]["capabilities"]


async def test_legacy_subscription_reaches_the_stream_it_did_not_open(registry):
    """An open stream reloads subscriptions written to shared session state."""
    registry.hub = hub = Hub()
    async with TestClient(TestServer(Endpoint(registry).app())) as client:
        session, caps = await handshake(client)
        assert caps["resources"]["subscribe"] is True

        subscribed = await legacy_post(
            client,
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "resources/subscribe",
                "params": {"uri": "config://app"},
            },
            session,
        )
        assert (await subscribed.json())["result"] == {}

        stream = await client.get(
            "/mcp",
            headers={
                "Accept": "text/event-stream",
                "MCP-Protocol-Version": LEGACY,
                "Mcp-Session-Id": session,
            },
        )
        assert stream.status == 200
        hub.publish("notifications/resources/updated", uri="other://thing")
        hub.publish("notifications/tools/list_changed")
        first = await event(stream)
        assert first["method"] == "notifications/tools/list_changed"
        hub.publish("notifications/resources/updated", uri="config://app")
        assert (await event(stream))["params"]["uri"] == "config://app"
        stream.close()


async def test_a_subscription_made_while_the_stream_is_open_takes_effect(registry):
    """Reload subscriptions each pass, including additions made after connection."""
    registry.hub = hub = Hub()
    async with TestClient(TestServer(Endpoint(registry).app())) as client:
        session, _ = await handshake(client)
        stream = await client.get(
            "/mcp",
            headers={
                "Accept": "text/event-stream",
                "MCP-Protocol-Version": LEGACY,
                "Mcp-Session-Id": session,
            },
        )
        await legacy_post(
            client,
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "resources/subscribe",
                "params": {"uri": "config://app"},
            },
            session,
        )
        hub.publish("notifications/resources/updated", uri="config://app")
        assert (await event(stream))["params"]["uri"] == "config://app"

        await legacy_post(
            client,
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "resources/unsubscribe",
                "params": {"uri": "config://app"},
            },
            session,
        )
        hub.publish("notifications/resources/updated", uri="config://app")
        hub.publish("notifications/prompts/list_changed")
        assert (await event(stream))["method"] == "notifications/prompts/list_changed"
        stream.close()


async def test_subscribing_to_nothing_is_an_error(registry):
    registry.hub = Hub()
    async with TestClient(TestServer(Endpoint(registry).app())) as client:
        session, _ = await handshake(client)
        response = await legacy_post(
            client,
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "resources/subscribe",
                "params": {"uri": "no://such/thing"},
            },
            session,
        )
        assert "error" in await response.json()


async def test_the_modern_revision_has_no_get_stream(registry):
    """Modern clients must use subscriptions/listen."""
    registry.hub = Hub()
    async with TestClient(TestServer(Endpoint(registry).app())) as client:
        stream = await client.get(
            "/mcp", headers={"Accept": "text/event-stream", "MCP-Protocol-Version": VERSION}
        )
        assert stream.status == 405
