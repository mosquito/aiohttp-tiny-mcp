"""Compression on the streams the server actually serves.

`tests/test_sse.py` checks `SSEResponse` on its own. These check the three
places that build one -- a streaming tool call, the legacy notification
stream, and the HTTP+SSE transport -- because a flag that reaches none of
them compresses nothing.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from aiohttp_tiny_mcp import Endpoint
from aiohttp_tiny_mcp.http_sse import SseEndpoint
from aiohttp_tiny_mcp.hub import NOTIFICATIONS, topic
from aiohttp_tiny_mcp.sse import read_sse

pytestmark = pytest.mark.asyncio

GZIP = {"Accept-Encoding": "gzip"}

CALL = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "tools/call",
    "params": {
        "name": "counter",
        "arguments": {"a": 1, "b": 4},
        "_meta": {
            "io.modelcontextprotocol/protocolVersion": "2026-07-28",
            "io.modelcontextprotocol/clientCapabilities": {},
        },
    },
}

MODERN = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
    "MCP-Protocol-Version": "2026-07-28",
    "Mcp-Method": "tools/call",
    "Mcp-Name": "counter",
}


async def serving(app: web.Application) -> TestClient:
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


async def test_a_streaming_call_is_compressed(registry):
    """The progress notifications and the result travel on one stream, which
    is the case that repeats its keys the most."""
    app = Endpoint(registry).app("/mcp")
    async with TestClient(TestServer(app)) as client:
        response = await client.post("/mcp", json=CALL, headers={**MODERN, **GZIP})
        assert response.headers["Content-Encoding"] == "gzip"
        events = [event async for event in read_sse(response)]
    bodies = [json.loads(event.data or "") for event in events]
    assert [body.get("method") for body in bodies[:-1]] == ["notifications/progress"] * 3
    assert bodies[-1]["result"]["content"][0]["text"] == "counted 3"


async def test_a_call_is_not_compressed_for_a_client_that_did_not_ask(registry):
    app = Endpoint(registry).app("/mcp")
    async with TestClient(TestServer(app)) as client:
        response = await client.post(
            "/mcp", json=CALL, headers={**MODERN, "Accept-Encoding": "identity"}
        )
        assert "Content-Encoding" not in response.headers
        events = [event async for event in read_sse(response)]
    assert json.loads(events[-1].data or "")["result"]["content"][0]["text"] == "counted 3"


async def test_compression_is_on_by_default(registry):
    """A client that cannot decompress does not ask for it, and a proxy doing
    it instead has to buffer, which is what puts pauses in a stream."""
    app = Endpoint(registry).app("/mcp")
    async with TestClient(TestServer(app)) as client:
        response = await client.post("/mcp", json=CALL, headers={**MODERN, **GZIP})
        assert response.headers["Content-Encoding"] == "gzip"
        events = [event async for event in read_sse(response)]
    assert json.loads(events[-1].data or "")["result"]["content"][0]["text"] == "counted 3"


async def test_compression_can_be_turned_off(registry):
    """For a deployment that compresses at the edge and would rather this did
    not."""
    app = Endpoint(registry, compress=False).app("/mcp")
    async with TestClient(TestServer(app)) as client:
        response = await client.post("/mcp", json=CALL, headers={**MODERN, **GZIP})
        assert "Content-Encoding" not in response.headers
        events = [event async for event in read_sse(response)]
    assert json.loads(events[-1].data or "")["result"]["content"][0]["text"] == "counted 3"


async def test_the_notification_stream_is_compressed(registry):
    """The legacy `GET` stream stays open for as long as the client does, so
    it is the one a proxy is most likely to cut without keep-alives.

    The stream is opened at a known id, so the event is published before
    the server looks and the test does not race it."""
    app = Endpoint(registry).app("/mcp")
    where = topic(NOTIFICATIONS)
    anchor = await registry.hub.publish(where, {"jsonrpc": "2.0", "method": "notifications/x"})
    published = {
        "jsonrpc": "2.0",
        "method": "notifications/tools/list_changed",
        "params": {},
    }
    await registry.hub.publish(where, published)
    async with TestClient(TestServer(app)) as client:
        opened = await client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2025-11-25", "capabilities": {}},
            },
            headers={"Accept": "application/json, text/event-stream"},
        )
        session = opened.headers["Mcp-Session-Id"]

        stream = await client.get(
            "/mcp",
            headers={
                "Accept": "text/event-stream",
                "Mcp-Session-Id": session,
                "MCP-Protocol-Version": "2025-11-25",
                "Last-Event-ID": anchor,
                **GZIP,
            },
        )
        assert stream.headers["Content-Encoding"] == "gzip"
        event = await read_sse(stream).__anext__()
        stream.close()
    assert json.loads(event.data or "")["method"] == "notifications/tools/list_changed"


async def test_the_old_transport_is_compressed(registry):
    """2024-11-05 clients are the oldest, and the likeliest to be on a link
    worth compressing."""
    app = web.Application()
    SseEndpoint(registry).setup(app)
    async with TestClient(TestServer(app)) as client:
        stream = await client.get("/sse", headers={"Accept": "text/event-stream", **GZIP})
        assert stream.headers["Content-Encoding"] == "gzip"
        event = await asyncio.wait_for(read_sse(stream).__anext__(), 5)
        stream.close()
    assert event.event == "endpoint"
    assert event.data is not None and event.data.startswith("/messages?session_id=")


async def test_a_call_says_the_same_thing_either_way(registry, enable_gzip):
    """Compression is a transfer detail: the events a client reads are the
    same whether or not it is on."""
    app = Endpoint(registry, compress=enable_gzip).app("/mcp")
    async with TestClient(TestServer(app)) as client:
        response = await client.post("/mcp", json=CALL, headers={**MODERN, **GZIP})
        assert ("Content-Encoding" in response.headers) is enable_gzip
        events = [event async for event in read_sse(response)]
    bodies = [json.loads(event.data or "") for event in events]
    assert [body.get("method") for body in bodies[:-1]] == ["notifications/progress"] * 3
    assert bodies[-1]["result"]["content"][0]["text"] == "counted 3"


async def test_the_old_transport_says_the_same_thing_either_way(registry, enable_gzip):
    app = web.Application()
    SseEndpoint(registry, compress=enable_gzip).setup(app)
    async with TestClient(TestServer(app)) as client:
        stream = await client.get("/sse", headers={"Accept": "text/event-stream", **GZIP})
        assert ("Content-Encoding" in stream.headers) is enable_gzip
        event = await asyncio.wait_for(read_sse(stream).__anext__(), 5)
        stream.close()
    assert event.event == "endpoint"
    assert event.data is not None and event.data.startswith("/messages?session_id=")
