"""Legacy stdio interoperability with the official SDK, including its required initialization
handshake.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from aiohttp_tiny_mcp.client.stdio import StdioClient
from aiohttp_tiny_mcp.protocol.models import CallToolResult, TextContent, TextResourceContents
from aiohttp_tiny_mcp.protocol.selection import AdapterSet

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(10)]

SCRIPT = str(Path(__file__).parent / "mcp_sdk_stdio_server_script.py")
LEGACY_ADAPTERS = [a for a in AdapterSet.default().adapters if a.version != "2026-07-28"]


@pytest.mark.parametrize("adapter", LEGACY_ADAPTERS, ids=lambda a: a.version)
async def test_handshake_and_list_tools(adapter):
    async with StdioClient.spawn(sys.executable, SCRIPT, adapter=adapter) as client:
        result = await client.initialize()
        assert result["serverInfo"]["name"] == "demo"
        assert result["protocolVersion"] == adapter.version

        tools = await client.list_tools()
        assert {"add", "boom"} <= {t.name for t in tools}


@pytest.mark.parametrize("adapter", LEGACY_ADAPTERS, ids=lambda a: a.version)
async def test_call_tool_success(adapter):
    async with StdioClient.spawn(sys.executable, SCRIPT, adapter=adapter) as client:
        await client.initialize()
        result = await client.call_tool("add", {"a": 2, "b": 3})
        assert isinstance(result, CallToolResult)
        assert result.is_error is False
        assert result.structured_content == {"result": 5}


@pytest.mark.parametrize("adapter", LEGACY_ADAPTERS, ids=lambda a: a.version)
async def test_call_tool_exception_is_a_result_not_an_error(adapter):
    async with StdioClient.spawn(sys.executable, SCRIPT, adapter=adapter) as client:
        await client.initialize()
        result = await client.call_tool("boom", {})
        assert isinstance(result, CallToolResult)
        assert result.is_error is True


@pytest.mark.parametrize("adapter", LEGACY_ADAPTERS, ids=lambda a: a.version)
async def test_resources(adapter):
    async with StdioClient.spawn(sys.executable, SCRIPT, adapter=adapter) as client:
        await client.initialize()
        resources = await client.list_resources()
        assert any(r.uri == "config://app" for r in resources)

        result = await client.read_resource("res://items/42")
        contents = result.contents[0]
        assert isinstance(contents, TextResourceContents)
        assert "item-42" in contents.text


@pytest.mark.parametrize("adapter", LEGACY_ADAPTERS, ids=lambda a: a.version)
async def test_prompts(adapter):
    async with StdioClient.spawn(sys.executable, SCRIPT, adapter=adapter) as client:
        await client.initialize()
        prompts = await client.list_prompts()
        assert {p.name for p in prompts} == {"greet"}

        result = await client.get_prompt("greet", {"language": "en"})
        content = result.messages[0].content
        assert isinstance(content, TextContent)
        assert "en speaker" in content.text


@pytest.mark.parametrize("adapter", LEGACY_ADAPTERS, ids=lambda a: a.version)
async def test_completion(adapter):
    async with StdioClient.spawn(sys.executable, SCRIPT, adapter=adapter) as client:
        await client.initialize()
        result = await client.complete(
            {"type": "ref/prompt", "name": "greet"}, {"name": "language", "value": "py"}
        )
        assert result.completion.values == ["python"]
