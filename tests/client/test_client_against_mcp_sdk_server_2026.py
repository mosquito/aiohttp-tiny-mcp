"""HTTP client interoperability with the official SDK on 2026-07-28."""

from __future__ import annotations

import pytest

from aiohttp_tiny_mcp import Client
from aiohttp_tiny_mcp.protocol.models import CallToolResult, TextContent, TextResourceContents
from aiohttp_tiny_mcp.protocol.selection import AdapterSet

pytestmark = pytest.mark.asyncio

ADAPTER = AdapterSet.default().by_version["2026-07-28"]


async def test_handshake_and_list_tools(mcp_sdk_server_url):
    async with Client(mcp_sdk_server_url, ADAPTER) as client:
        result = await client.initialize()
        assert result["_meta"]["io.modelcontextprotocol/serverInfo"]["name"] == "demo"
        assert result["resultType"] == "complete"

        tools = await client.list_tools()
        assert {"add", "boom"} <= {t.name for t in tools}


async def test_call_tool_success(mcp_sdk_server_url):
    async with Client(mcp_sdk_server_url, ADAPTER) as client:
        await client.initialize()
        result = await client.call_tool("add", {"a": 2, "b": 3})
        assert isinstance(result, CallToolResult)
        assert result.is_error is False
        assert result.structured_content == {"result": 5}


async def test_call_tool_exception_is_a_result_not_an_error(mcp_sdk_server_url):
    async with Client(mcp_sdk_server_url, ADAPTER) as client:
        await client.initialize()
        result = await client.call_tool("boom", {})
        assert isinstance(result, CallToolResult)
        assert result.is_error is True


async def test_resources(mcp_sdk_server_url):
    async with Client(mcp_sdk_server_url, ADAPTER) as client:
        await client.initialize()
        resources = await client.list_resources()
        assert any(r.uri == "config://app" for r in resources)

        result = await client.read_resource("res://items/42")
        contents = result.contents[0]
        assert isinstance(contents, TextResourceContents)
        assert "item-42" in contents.text


async def test_prompts(mcp_sdk_server_url):
    async with Client(mcp_sdk_server_url, ADAPTER) as client:
        await client.initialize()
        prompts = await client.list_prompts()
        assert {p.name for p in prompts} == {"greet"}

        result = await client.get_prompt("greet", {"language": "en"})
        content = result.messages[0].content
        assert isinstance(content, TextContent)
        assert "en speaker" in content.text


async def test_completion(mcp_sdk_server_url):
    async with Client(mcp_sdk_server_url, ADAPTER) as client:
        await client.initialize()
        result = await client.complete(
            {"type": "ref/prompt", "name": "greet"}, {"name": "language", "value": "py"}
        )
        assert result.completion.values == ["python"]
