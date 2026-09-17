"""The transport a 2024-11-05 client speaks over HTTP.

Driven the way the specification says a client drives it: open the stream,
read the `endpoint` event, post everything to the address it names, and read
every reply off the stream that was opened first.

Deliberately not driven with the bundled `Client`, which speaks Streamable
HTTP. A test that used it would be checking the transport against itself.
"""

from __future__ import annotations

import asyncio
import json
from urllib.parse import parse_qs, urlsplit

import aiohttp
import pytest
from aiohttp import web
from pydantic import BaseModel

from aiohttp_tiny_mcp import (
    Exchange,
    MemoryHub,
    MemorySessionStore,
    Registry,
    SseEndpoint,
    elicit,
)

pytestmark = pytest.mark.asyncio


class Add(BaseModel):
    a: int
    b: int


class Nothing(BaseModel):
    pass


@pytest.fixture
def offered() -> Registry:
    registry = Registry("old", "1.0", hub=MemoryHub(), session_store=MemorySessionStore())

    @registry.tool
    async def add(args: Add) -> int:
        """Add two integers."""
        return args.a + args.b

    @registry.tool(streaming=True)
    async def counting(args: Nothing, ex: Exchange) -> str:
        """Reports before it answers."""
        await ex.progress(1, 2)
        await ex.log("warning", "halfway")
        return "counted"

    @registry.tool
    async def gated(args: Nothing, ex: Exchange) -> str:
        """Asks, on a revision two releases older than elicitation."""
        agreed = await ex.ask("confirm", elicit("Go ahead?"))
        return "went ahead" if agreed.accepted else "stopped"

    return registry


@pytest.fixture
async def served(offered):
    app = web.Application()
    SseEndpoint(offered).setup(app)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    host, port = runner.addresses[0]
    try:
        yield f"http://{host}:{port}"
    finally:
        await runner.cleanup()


class OldClient:
    """A 2024-11-05 client, in as few lines as the transport allows."""

    def __init__(self, base: str) -> None:
        self.base = base
        self.http = aiohttp.ClientSession()
        self.stream = None
        self.post_to = ""
        self.seen: asyncio.Queue = asyncio.Queue()
        self.reading: asyncio.Task | None = None

    async def open(self) -> str:
        self.stream = await self.http.get(
            f"{self.base}/sse", headers={"Accept": "text/event-stream"}
        )
        event, data = await self.next_event()
        assert event == "endpoint", f"the first event must be `endpoint`, not {event}"
        self.post_to = data
        self.reading = asyncio.ensure_future(self.read())
        return data

    async def next_event(self) -> tuple[str, str]:
        event = ""
        async for raw in self.stream.content:
            line = raw.decode().strip()
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:"):
                return event, line[5:].strip()
        raise AssertionError("the stream ended")

    async def read(self) -> None:
        while True:
            event, data = await self.next_event()
            await self.seen.put(json.loads(data))

    async def send(self, method: str, params: dict, request_id: int | None = 1) -> None:
        envelope = {"jsonrpc": "2.0", "method": method, "params": params}
        if request_id is not None:
            envelope["id"] = request_id
        async with self.http.post(f"{self.base}{self.post_to}", json=envelope) as response:
            assert response.status == 202, await response.text()

    async def call(self, method: str, params: dict, request_id: int = 1) -> dict:
        await self.send(method, params, request_id)
        while True:
            message = await asyncio.wait_for(self.seen.get(), 5)
            if message.get("id") == request_id:
                return message

    async def close(self) -> None:
        if self.reading is not None:
            self.reading.cancel()
        if self.stream is not None:
            self.stream.close()
        await self.http.close()


@pytest.fixture
async def old(served):
    client = OldClient(served)
    await client.open()
    try:
        yield client
    finally:
        await client.close()


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
