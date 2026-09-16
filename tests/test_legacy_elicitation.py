"""Legacy questions pushed on an active stream and answered through a separate request."""

from __future__ import annotations

import asyncio
import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from aiohttp_tiny_mcp import Endpoint, namespace

pytestmark = pytest.mark.asyncio


async def handshake(client, capabilities, version="2025-11-25"):
    response = await client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": version, "capabilities": capabilities},
        },
    )
    return response.headers.get("Mcp-Session-Id")


async def call(client, session, tool="gated", version="2025-11-25"):
    return await client.post(
        "/mcp",
        headers={"Mcp-Session-Id": session, "MCP-Protocol-Version": version},
        json={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": tool, "arguments": {}},
        },
    )


async def frame(response, seconds: float = 5.0):
    """Read one event the way the standard says a client reads one.

    A blank line dispatches, a line opening with a colon is a comment, and
    several `data` fields join with newlines. Reading it this way is also what
    checks that the server frames it that way.
    """
    data: list[str] = []
    while True:
        raw = await asyncio.wait_for(response.content.readline(), seconds)
        line = raw.decode().rstrip("\r\n")
        if not line:
            if data:
                return json.loads("\n".join(data))
            continue  # a comment or padding between events
        if line.startswith(":"):
            continue
        field, _, value = line.partition(":")
        if field == "data":
            data.append(value[1:] if value.startswith(" ") else value)


async def answer(client, session, wire_id, reply, version="2025-11-25"):
    return await client.post(
        "/mcp",
        headers={"Mcp-Session-Id": session, "MCP-Protocol-Version": version},
        json={"jsonrpc": "2.0", "id": wire_id, "result": reply},
    )


@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        ({"action": "accept", "content": {}}, "went ahead"),
        ({"action": "decline"}, "stopped at decline"),
        ({"action": "cancel"}, "stopped at cancel"),
    ],
)
async def test_pushed_question_round_trip(client, reply, expected):
    session = await handshake(client, {"elicitation": {}})
    response = await call(client, session)
    assert response.headers["Content-Type"].startswith("text/event-stream")

    pushed = await frame(response)
    assert pushed["method"] == "elicitation/create"

    assert (await answer(client, session, pushed["id"], reply)).status == 202
    assert (await frame(response))["result"]["content"][0]["text"] == expected
    response.close()


async def test_a_client_that_declared_nothing_is_not_asked(client):
    """Legacy elicitation capability must be retained from the handshake."""
    session = await handshake(client, {})
    response = await call(client, session)
    assert response.headers["Content-Type"].startswith("application/json")
    body = await response.json()
    assert body["error"]["code"] == -32603


async def test_2025_03_26_asks_in_the_tool_call(client):
    """Pre-elicitation clients receive questions as successful tool results."""
    session = await handshake(client, {"elicitation": {}}, version="2025-03-26")
    response = await call(client, session, version="2025-03-26")
    body = await response.json()
    assert "error" not in body
    assert body["result"]["isError"] is False
    text = body["result"]["content"][0]["text"]
    assert "mcpAnswers" in text
    assert "go ahead?" in text


async def test_a_default_is_not_used_where_the_question_can_be_asked(client):
    session = await handshake(client, {}, version="2025-03-26")
    response = await call(client, session, tool="defaulted", version="2025-03-26")
    body = await response.json()
    assert "mcpAnswers" in body["result"]["content"][0]["text"]


@pytest.mark.timeout(2)
async def test_an_answer_from_another_caller_reaches_nobody(registry):
    """Namespaces isolate reply topics. Set them in server middleware, not in the test task."""

    @web.middleware
    async def tenant(request, handler):
        namespace.set(request.headers.get("X-Tenant"))
        return await handler(request)

    app = web.Application(middlewares=[tenant])
    Endpoint(registry).setup(app, "/mcp")
    async with TestClient(TestServer(app)) as client:
        owner = {"X-Tenant": "owner"}
        response = await client.post(
            "/mcp",
            headers=owner,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {"elicitation": {}},
                },
            },
        )
        session = response.headers["Mcp-Session-Id"]

        asking = await client.post(
            "/mcp",
            headers={**owner, "Mcp-Session-Id": session, "MCP-Protocol-Version": "2025-11-25"},
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "gated", "arguments": {}},
            },
        )
        pushed = await frame(asking)

        intruder = await client.post(
            "/mcp",
            headers={"X-Tenant": "intruder", "MCP-Protocol-Version": "2025-11-25"},
            json={"jsonrpc": "2.0", "id": pushed["id"], "result": {"action": "accept"}},
        )
        assert intruder.status == 202  # accepted, and delivered to nobody

        with pytest.raises(asyncio.TimeoutError):
            await frame(asking, seconds=0.3)
        asking.close()


@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        ({"action": "accept", "content": {}}, "accepted"),
        ({"action": "decline"}, "refused:decline"),
    ],
)
async def test_a_handler_that_raises_need_input_is_asked_the_same_way(client, reply, expected):
    """`NeedInput` is the explicit form of what `ask` raises for the handler,
    so it has to reach the client the same way.

    These revisions have no result that can carry a question, so the question
    goes on the open stream and the handler runs again. Without that, the
    encoder is handed an outcome the revision cannot express, the request ends
    with no reply at all, and the client waits for one that never comes.
    """
    session = await handshake(client, {"elicitation": {}})
    response = await call(client, session, tool="gate")
    assert response.headers["Content-Type"].startswith("text/event-stream")

    pushed = await frame(response)
    assert pushed["method"] == "elicitation/create"

    assert (await answer(client, session, pushed["id"], reply)).status == 202
    assert (await frame(response))["result"]["content"][0]["text"] == expected
    response.close()
