"""Stdio client/server round trips through a subprocess across all revisions."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

from aiohttp_tiny_mcp import ClientError, elicit_accept
from aiohttp_tiny_mcp.models import CallToolResult, TextContent, TextResourceContents
from aiohttp_tiny_mcp.protocol.selection import AdapterSet
from aiohttp_tiny_mcp.stdio_client import StdioClient

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(10)]

SCRIPT = str(Path(__file__).parent / "stdio_server_script.py")
ADAPTERS = AdapterSet.default().adapters


@pytest.mark.parametrize("adapter", ADAPTERS, ids=lambda a: a.version)
async def test_handshake_and_list_tools(adapter):
    async with StdioClient.spawn(sys.executable, SCRIPT, adapter=adapter) as client:
        result = await client.initialize()
        if adapter.version == "2026-07-28":
            assert result["_meta"]["io.modelcontextprotocol/serverInfo"]["name"] == "demo"
            assert adapter.version in result["supportedVersions"]
        else:
            assert result["serverInfo"]["name"] == "demo"
            assert result["protocolVersion"] == adapter.version

        tools = await client.list_tools()
        assert {"add", "boom", "confirm"} <= {t.name for t in tools}


@pytest.mark.parametrize("adapter", ADAPTERS, ids=lambda a: a.version)
async def test_call_tool_success(adapter):
    async with StdioClient.spawn(sys.executable, SCRIPT, adapter=adapter) as client:
        await client.initialize()
        result = await client.call_tool("add", {"a": 2, "b": 3})
        assert isinstance(result, CallToolResult)
        assert result.is_error is False
        assert result.structured_content == {"result": 5}


@pytest.mark.parametrize("adapter", ADAPTERS, ids=lambda a: a.version)
async def test_call_tool_bad_arguments_is_a_result_not_an_error(adapter):
    async with StdioClient.spawn(sys.executable, SCRIPT, adapter=adapter) as client:
        await client.initialize()
        result = await client.call_tool("add", {"a": "not-a-number"})
        assert isinstance(result, CallToolResult)
        assert result.is_error is True


@pytest.mark.parametrize("adapter", ADAPTERS, ids=lambda a: a.version)
async def test_call_tool_exception_is_a_result_not_an_error(adapter):
    async with StdioClient.spawn(sys.executable, SCRIPT, adapter=adapter) as client:
        await client.initialize()
        result = await client.call_tool("boom", {})
        assert isinstance(result, CallToolResult)
        assert result.is_error is True
        content = result.content[0]
        assert isinstance(content, TextContent)
        assert "kaboom" in content.text


@pytest.mark.parametrize("adapter", ADAPTERS, ids=lambda a: a.version)
async def test_resources(adapter):
    async with StdioClient.spawn(sys.executable, SCRIPT, adapter=adapter) as client:
        await client.initialize()
        resources = await client.list_resources()
        assert any(r.uri == "config://app" for r in resources)

        result = await client.read_resource("res://items/42")
        contents = result.contents[0]
        assert isinstance(contents, TextResourceContents)
        assert "item-42" in contents.text


@pytest.mark.parametrize("adapter", ADAPTERS, ids=lambda a: a.version)
async def test_prompts(adapter):
    async with StdioClient.spawn(sys.executable, SCRIPT, adapter=adapter) as client:
        await client.initialize()
        prompts = await client.list_prompts()
        assert {p.name for p in prompts} == {"greet"}

        result = await client.get_prompt("greet", {"language": "en"})
        content = result.messages[0].content
        assert isinstance(content, TextContent)
        assert "en speaker" in content.text


@pytest.mark.parametrize("adapter", ADAPTERS, ids=lambda a: a.version)
async def test_completion(adapter):
    async with StdioClient.spawn(sys.executable, SCRIPT, adapter=adapter) as client:
        await client.initialize()
        result = await client.complete(
            {"type": "ref/prompt", "name": "greet"}, {"name": "language", "value": "py"}
        )
        assert result.completion.values == ["python"]


class FakeWriter:
    def __init__(self) -> None:
        self.lines: list[bytes] = []

    def write(self, data: bytes) -> None:
        self.lines.append(data)

    async def drain(self) -> None:
        pass


async def test_exchange_yields_every_frame_as_it_arrives():
    """Notifications may precede the reply on the same channel."""
    reader = asyncio.StreamReader()
    reader.feed_data(
        (
            json.dumps({"jsonrpc": "2.0", "method": "notifications/progress", "params": {}}) + "\n"
        ).encode()
    )
    reader.feed_data(
        (json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"ok": True}}) + "\n").encode()
    )
    reader.feed_eof()

    adapter = AdapterSet.default().by_version["2025-11-25"]
    client = StdioClient(reader, FakeWriter(), adapter)
    seen = []
    async for frame in client.exchange(
        {"jsonrpc": "2.0", "id": 1, "method": "ping"}, method="ping", name=None
    ):
        seen.append(frame)
        if frame.get("id") == 1:
            break
    assert seen == [
        {"jsonrpc": "2.0", "method": "notifications/progress", "params": {}},
        {"jsonrpc": "2.0", "id": 1, "result": {"ok": True}},
    ]


async def test_mrtr_unsupported_on_legacy():
    adapter = AdapterSet.default().by_version["2025-11-25"]
    async with StdioClient.spawn(sys.executable, SCRIPT, adapter=adapter) as client:
        await client.initialize()
        result = await client.call_tool("confirm", {"service": "x"})
        assert isinstance(result, CallToolResult)
        assert result.is_error is False
        content = result.content[0]
        assert isinstance(content, TextContent)
        assert "cannot ask" in content.text


async def test_mrtr_round_trip_on_2026_07_28():
    adapter = AdapterSet.default().by_version["2026-07-28"]
    async with StdioClient.spawn(sys.executable, SCRIPT, adapter=adapter) as client:
        await client.initialize()
        first = await client.call_tool("confirm", {"service": "stdio"})
        assert isinstance(first, dict)
        assert first["resultType"] == "input_required"

        with pytest.raises(ClientError) as invalid:
            await client.call_tool(
                "confirm", {"service": "other"}, request_state=first["requestState"]
            )
        assert invalid.value.code == -32602

        second = await client.call_tool(
            "confirm",
            {"service": "stdio"},
            input_responses={"confirm": elicit_accept({"ok": True})},
            request_state=first["requestState"],
        )
        assert isinstance(second, CallToolResult)
        content = second.content[0]
        assert isinstance(content, TextContent)
        assert "deployed stdio" in content.text


@pytest.mark.parametrize(
    "adapter", [a for a in ADAPTERS if a.can_ask or a.can_push_ask], ids=lambda a: a.version
)
async def test_one_question_is_answered_over_stdio(adapter):
    """Answers share the stdio channel and must bypass request decoding."""
    seen = []

    async def on_ask(request):
        seen.append(request["params"]["message"])
        return elicit_accept({})

    async with StdioClient.spawn(sys.executable, SCRIPT, adapter=adapter, on_ask=on_ask) as client:
        await client.initialize()
        result = await client.call_tool("gated", {})
    assert seen == ["go ahead?"]
    assert isinstance(result, CallToolResult)
    content = result.content[0]
    assert isinstance(content, TextContent)
    assert content.text == "went ahead"
