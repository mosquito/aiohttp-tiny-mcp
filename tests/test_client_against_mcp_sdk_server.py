"""HTTP client interoperability with the official SDK across legacy revisions."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest
import uvicorn
from mcp.server.mcpserver import MCPServer
from mcp_types import Completion
from pydantic import BaseModel

from aiohttp_tiny_mcp import Client
from aiohttp_tiny_mcp.models import CallToolResult, TextContent, TextResourceContents
from aiohttp_tiny_mcp.protocol.selection import AdapterSet

pytestmark = pytest.mark.asyncio

LEGACY_ADAPTERS = [a for a in AdapterSet.default().adapters if a.version != "2026-07-28"]


class Sum(BaseModel):
    result: int


def build_fastmcp_app():
    server = MCPServer("demo")

    @server.tool()
    def add(a: int, b: int) -> Sum:
        return Sum(result=a + b)

    @server.tool()
    def boom() -> str:
        raise RuntimeError("kaboom")

    @server.resource("config://app")
    def config() -> dict:
        return {"debug": False}

    @server.resource("res://items/{id}")
    def item(id: str) -> str:
        return f"item-{id}"

    @server.prompt()
    def greet(language: str) -> str:
        return f"Hello, {language} speaker."

    @server.completion()
    async def complete(ref, argument, context):
        if argument.name == "language":
            values = [x for x in ("python", "rust", "go") if x.startswith(argument.value)]
            return Completion(values=values)
        return Completion(values=[])

    return server.streamable_http_app(stateless_http=True)


@pytest.fixture
async def mcp_sdk_server_url() -> AsyncIterator[str]:
    app = build_fastmcp_app()
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
    sock = config.bind_socket()
    port = sock.getsockname()[1]
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        while not server.started:
            await asyncio.sleep(0.01)
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        server.should_exit = True
        await task


@pytest.mark.parametrize("adapter", LEGACY_ADAPTERS, ids=lambda a: a.version)
async def test_handshake_and_list_tools(mcp_sdk_server_url, adapter):
    async with Client(mcp_sdk_server_url, adapter) as client:
        result = await client.initialize()
        assert result["serverInfo"]["name"] == "demo"
        assert result["protocolVersion"] == adapter.version

        tools = await client.list_tools()
        assert {"add", "boom"} <= {t.name for t in tools}


@pytest.mark.parametrize("adapter", LEGACY_ADAPTERS, ids=lambda a: a.version)
async def test_call_tool_success(mcp_sdk_server_url, adapter):
    async with Client(mcp_sdk_server_url, adapter) as client:
        await client.initialize()
        result = await client.call_tool("add", {"a": 2, "b": 3})
        assert isinstance(result, CallToolResult)
        assert result.is_error is False
        assert result.structured_content == {"result": 5}


@pytest.mark.parametrize("adapter", LEGACY_ADAPTERS, ids=lambda a: a.version)
async def test_call_tool_exception_is_a_result_not_an_error(mcp_sdk_server_url, adapter):
    async with Client(mcp_sdk_server_url, adapter) as client:
        await client.initialize()
        result = await client.call_tool("boom", {})
        assert isinstance(result, CallToolResult)
        assert result.is_error is True


@pytest.mark.parametrize("adapter", LEGACY_ADAPTERS, ids=lambda a: a.version)
async def test_resources(mcp_sdk_server_url, adapter):
    async with Client(mcp_sdk_server_url, adapter) as client:
        await client.initialize()
        resources = await client.list_resources()
        assert any(r.uri == "config://app" for r in resources)

        result = await client.read_resource("res://items/42")
        contents = result.contents[0]
        assert isinstance(contents, TextResourceContents)
        assert "item-42" in contents.text


@pytest.mark.parametrize("adapter", LEGACY_ADAPTERS, ids=lambda a: a.version)
async def test_prompts(mcp_sdk_server_url, adapter):
    async with Client(mcp_sdk_server_url, adapter) as client:
        await client.initialize()
        prompts = await client.list_prompts()
        assert {p.name for p in prompts} == {"greet"}

        result = await client.get_prompt("greet", {"language": "en"})
        content = result.messages[0].content
        assert isinstance(content, TextContent)
        assert "en speaker" in content.text


@pytest.mark.parametrize("adapter", LEGACY_ADAPTERS, ids=lambda a: a.version)
async def test_completion(mcp_sdk_server_url, adapter):
    async with Client(mcp_sdk_server_url, adapter) as client:
        await client.initialize()
        result = await client.complete(
            {"type": "ref/prompt", "name": "greet"}, {"name": "language", "value": "py"}
        )
        assert result.completion.values == ["python"]
