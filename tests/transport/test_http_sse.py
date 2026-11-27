"""The transport a 2024-11-05 client speaks over HTTP.

Driven the way the specification says a client drives it: open the stream,
read the `endpoint` event, post everything to the address it names, and read
every reply off the stream that was opened first.

Deliberately not driven with the bundled `Client`, which speaks Streamable
HTTP. A test that used it would be checking the transport against itself.
"""

from __future__ import annotations

import asyncio
from urllib.parse import parse_qs, urlsplit

import aiohttp
import pytest
from aiohttp import web

from aiohttp_tiny_mcp import (
    SseEndpoint,
)
from tests.helpers import OldClient

pytestmark = pytest.mark.asyncio


async def test_the_first_event_names_where_to_post(served):
    client = OldClient(served)
    try:
        where = await client.open()
    finally:
        await client.close()

    parts = urlsplit(where)
    assert parts.path == "/messages"
    assert parse_qs(parts.query)["session_id"]


async def test_a_request_is_answered_on_the_stream(old):
    answered = await old.call(
        "initialize",
        {
            "protocolVersion": "2024-11-05",
            "clientInfo": {"name": "old", "version": "1"},
            "capabilities": {},
        },
    )
    assert answered["result"]["protocolVersion"] == "2024-11-05"

    listed = await old.call("tools/list", {}, request_id=2)
    assert {tool["name"] for tool in listed["result"]["tools"]} == {"add", "counting", "gated"}

    called = await old.call("tools/call", {"name": "add", "arguments": {"a": 2, "b": 3}}, 3)
    assert called["result"]["content"][0]["text"] == "5"
    assert "structuredContent" not in called["result"]


async def test_a_notification_is_accepted_and_answered_with_nothing(old):
    await old.send("notifications/initialized", {}, request_id=None)
    answered = await old.call("tools/list", {}, request_id=7)
    assert answered["id"] == 7


async def test_progress_and_logging_reach_the_stream(old):
    await old.call(
        "initialize",
        {
            "protocolVersion": "2024-11-05",
            "clientInfo": {"name": "o", "version": "1"},
            "capabilities": {},
        },
    )
    await old.call("logging/setLevel", {"level": "debug"}, request_id=2)
    await old.send("tools/call", {"name": "counting", "arguments": {}}, request_id=3)

    seen = []
    while True:
        message = await asyncio.wait_for(old.seen.get(), 5)
        seen.append(message)
        if message.get("id") == 3:
            break

    methods = [message.get("method") for message in seen if "method" in message]
    assert "notifications/progress" in methods
    assert "notifications/message" in methods
    assert seen[-1]["result"]["content"][0]["text"] == "counted"


async def test_a_question_is_asked_the_way_this_revision_can(old):
    """Elicitation is two releases away. The convention carries it."""
    asked = await old.call("tools/call", {"name": "gated", "arguments": {}}, request_id=4)
    text = asked["result"]["content"][0]["text"]
    assert "mcpAnswers" in text

    carried = asked["result"]["_meta"]["dev.aiohttp-tiny-mcp/inputRequired"]
    answered = await old.call(
        "tools/call",
        {
            "name": "gated",
            "arguments": {
                "mcpAnswers": {"confirm": {"action": "accept", "content": {}}},
                "mcpState": carried["requestState"],
            },
        },
        request_id=5,
    )
    assert answered["result"]["content"][0]["text"] == "went ahead"


async def test_posting_to_a_session_that_is_gone_is_refused(served):
    async with aiohttp.ClientSession() as http:
        async with http.post(
            f"{served}/messages?session_id=never-opened",
            json={"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {}},
        ) as response:
            assert response.status == 404


async def test_a_malformed_message_is_answered_rather_than_dropped(old):
    async with aiohttp.ClientSession() as http:
        async with http.post(f"{old.base}{old.post_to}", data=b"{not json") as response:
            assert response.status == 202
    failure = await asyncio.wait_for(old.seen.get(), 5)
    assert "error" in failure


async def test_a_message_posted_to_another_worker_is_still_answered(offered):
    """Nothing pins the POST to the worker holding the stream. Two endpoints
    over one registry stand in for two workers sharing a store and a hub."""
    apps = []
    for _ in range(2):
        app = web.Application()
        SseEndpoint(offered).setup(app)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        host, port = runner.addresses[0]
        apps.append((runner, f"http://{host}:{port}"))

    listening, posting = apps[0][1], apps[1][1]
    client = OldClient(listening)
    try:
        where = await client.open()
        async with aiohttp.ClientSession() as http:
            async with http.post(
                f"{posting}{where}",
                json={
                    "jsonrpc": "2.0",
                    "id": 9,
                    "method": "tools/call",
                    "params": {"name": "add", "arguments": {"a": 20, "b": 22}},
                },
            ) as response:
                assert response.status == 202

        while True:
            message = await asyncio.wait_for(client.seen.get(), 5)
            if message.get("id") == 9:
                break
    finally:
        await client.close()
        for runner, _ in apps:
            await runner.cleanup()

    assert message["result"]["content"][0]["text"] == "42"


async def test_a_listing_pages_over_this_transport_too(offered):
    """Paging is the dispatcher's, so the oldest transport has it too."""
    offered.page_size = 1
    app = web.Application()
    SseEndpoint(offered).setup(app)
    runner = web.AppRunner(app)
    await runner.setup()
    try:
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        host, port = runner.addresses[0]
        client = OldClient(f"http://{host}:{port}")
        await client.open()
        try:
            seen: list[str] = []
            cursor = None
            pages = 0
            while True:
                params = {"cursor": cursor} if cursor else {}
                answered = await client.call("tools/list", params, request_id=100 + pages)
                result = answered["result"]
                seen += [tool["name"] for tool in result["tools"]]
                pages += 1
                cursor = result.get("nextCursor")
                if not cursor:
                    break
        finally:
            await client.close()
    finally:
        await runner.cleanup()

    assert seen == sorted(seen)
    assert len(seen) == len(set(seen)), "no entry arrived twice"
    assert pages == len(seen), "one at a time, as the page size says"


async def test_a_pushed_question_is_answered_through_the_message_endpoint(old):
    """A revision that pushes a question must be able to receive the answer here.

    Two things were missing. This transport opens the session before the handshake, so
    the capabilities the client declared were never stored and the server refused to ask
    anything. And the answer is a JSON-RPC response, with no method and no handler, which
    this endpoint decoded as a request.

    The endpoint answers 202 before the handler returns. The client answers the
    question on another POST while it reads the result from the stream.
    """
    await old.call(
        "initialize",
        {
            "protocolVersion": "2025-06-18",
            "clientInfo": {"name": "asker", "version": "1"},
            "capabilities": {"elicitation": {}},
        },
    )
    posting = asyncio.ensure_future(
        old.send("tools/call", {"name": "gated", "arguments": {}}, request_id=4)
    )
    try:
        while True:
            pushed = await asyncio.wait_for(old.seen.get(), 5)
            if pushed.get("method") == "elicitation/create":
                break
        assert pushed["params"]["message"] == "Go ahead?"

        await old.raw(
            {"jsonrpc": "2.0", "id": pushed["id"], "result": {"action": "accept", "content": {}}}
        )

        while True:
            answered = await asyncio.wait_for(old.seen.get(), 5)
            if answered.get("id") == 4:
                break
    finally:
        posting.cancel()
    assert answered["result"]["content"][0]["text"] == "went ahead"


async def test_a_declined_question_reaches_the_handler(old):
    """A refusal is an answer, so the handler continues with it."""
    await old.call(
        "initialize",
        {
            "protocolVersion": "2025-06-18",
            "clientInfo": {"name": "asker", "version": "1"},
            "capabilities": {"elicitation": {}},
        },
    )
    posting = asyncio.ensure_future(
        old.send("tools/call", {"name": "gated", "arguments": {}}, request_id=5)
    )
    try:
        while True:
            pushed = await asyncio.wait_for(old.seen.get(), 5)
            if pushed.get("method") == "elicitation/create":
                break
        await old.raw({"jsonrpc": "2.0", "id": pushed["id"], "result": {"action": "decline"}})
        while True:
            answered = await asyncio.wait_for(old.seen.get(), 5)
            if answered.get("id") == 5:
                break
    finally:
        posting.cancel()
    assert answered["result"]["content"][0]["text"] == "stopped"


async def test_the_handshake_capabilities_are_remembered_for_later_requests(old):
    """Without the stored capabilities the server reads a client that declared nothing."""
    await old.call(
        "initialize",
        {
            "protocolVersion": "2025-06-18",
            "clientInfo": {"name": "asker", "version": "1"},
            "capabilities": {},
        },
    )
    answered = await old.call("tools/call", {"name": "gated", "arguments": {}}, request_id=6)
    # The stored declaration is what the server reads, so a client that declared nothing
    # is refused instead of being asked, and told what to declare.
    assert answered["error"]["message"] == "client did not declare the elicitation capability"
    assert answered["error"]["data"] == {"requiredCapabilities": {"elicitation": {}}}
