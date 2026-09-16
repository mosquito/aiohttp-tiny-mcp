"""HTTP client behavior against custom JSON-RPC and SSE servers."""

from __future__ import annotations

import asyncio
import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from aiohttp_tiny_mcp import Client, ClientError
from aiohttp_tiny_mcp.protocol.selection import AdapterSet

pytestmark = pytest.mark.asyncio

LEGACY = AdapterSet.default().by_version["2025-11-25"]


async def serve(handler):
    server = TestServer(web.Application())
    server.app.router.add_post("/mcp", handler)
    await server.start_server()
    return server, str(server.make_url("/mcp"))


async def test_the_session_a_server_issues_is_returned_on_every_later_request():
    seen: list[str | None] = []

    async def handler(request):
        body = await request.json()
        seen.append(request.headers.get("Mcp-Session-Id"))
        headers = {}
        if body["method"] == "initialize":
            headers["Mcp-Session-Id"] = "s-1"
        elif seen[-1] != "s-1":
            return web.json_response({"error": "no session"}, status=404)
        if "id" not in body:
            return web.Response(status=202, headers=headers)
        result = {} if body["method"] == "initialize" else {"prompts": []}
        return web.json_response(
            {"jsonrpc": "2.0", "id": body["id"], "result": result}, headers=headers
        )

    server, url = await serve(handler)
    try:
        async with Client(url, LEGACY) as client:
            await client.initialize()
            await client.list_prompts()
            await client.list_prompts()
    finally:
        await server.close()
    assert seen[0] is None, "nothing to send before the server has issued one"
    assert seen[1:] == ["s-1"] * (len(seen) - 1)


async def test_a_reply_is_read_before_the_stream_ends():
    """The server may keep SSE open after the reply; waiting for EOF would hang."""

    hang = asyncio.Event()

    async def handler(request):
        body = await request.json()
        if "id" not in body:
            return web.Response(status=202)
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await response.prepare(request)
        reply = {"jsonrpc": "2.0", "id": body["id"], "result": {}}
        await response.write(f"data: {json.dumps(reply)}\r\n\r\n".encode())
        await hang.wait()
        return response

    server, url = await serve(handler)
    try:
        async with Client(url, LEGACY) as client:
            await asyncio.wait_for(client.initialize(), 2)
    finally:
        hang.set()
        await server.close()


async def test_a_refusal_with_no_id_is_still_the_reply():
    """Pre-decode errors have id=null and must surface as errors rather than timeouts."""

    async def handler(request):
        return web.json_response(
            {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32022, "message": "no", "data": {"supported": ["x"]}},
            },
            status=400,
        )

    server, url = await serve(handler)
    try:
        async with Client(url, LEGACY) as client:
            with pytest.raises(ClientError) as raised:
                await client.initialize()
    finally:
        await server.close()
    assert raised.value.code == -32022
    assert raised.value.data == {"supported": ["x"]}


async def test_a_data_line_with_no_payload_does_not_end_the_read():
    """SSE servers prime and keep streams alive with an empty `data:` line.

    The official SDK writes one before the first reply when an event store is
    configured. Parsing it as JSON would fail before the reply is read.
    """

    async def handler(request):
        body = await request.json()
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await response.prepare(request)
        await response.write(b"id: 1@5\r\ndata: \r\n\r\n")
        reply = json.dumps({"jsonrpc": "2.0", "id": body["id"], "result": {"prompts": []}})
        await response.write(f"event: message\r\ndata: {reply}\r\n\r\n".encode())
        await response.write_eof()
        return response

    server, url = await serve(handler)
    try:
        async with Client(url, LEGACY) as client:
            assert await client.list_prompts() == []
    finally:
        await server.close()
