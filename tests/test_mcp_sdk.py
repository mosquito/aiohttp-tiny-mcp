"""Server interoperability with the official legacy SDK client over HTTP."""

from __future__ import annotations

import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPDeprecationWarning
from mcp_types import PromptReference, TextContent, TextResourceContents

pytestmark = pytest.mark.asyncio


async def test_initialize(real_server_url):
    async with streamable_http_client(real_server_url) as (read, write):
        async with ClientSession(read, write) as session:
            result = await session.initialize()
            assert result.server_info.name == "demo"
            assert result.protocol_version == "2025-11-25"


async def test_list_and_call_tool(real_server_url):
    async with streamable_http_client(real_server_url) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            tools = await session.list_tools()
            names = {t.name for t in tools.tools}
            assert {"add", "boom", "confirm", "counter"} <= names

            result = await session.call_tool("add", {"a": 2, "b": 3})
            assert result.is_error is False
            assert result.structured_content == {"result": 5}


async def test_call_tool_bad_arguments_is_a_result_not_an_error(real_server_url):
    """The SDK must receive isError=true rather than raise McpError."""
    async with streamable_http_client(real_server_url) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool("add", {"a": "not-a-number"})
            assert result.is_error is True


async def test_resources(real_server_url):
    async with streamable_http_client(real_server_url) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            resources = await session.list_resources()
            assert any(r.uri == "config://app" for r in resources.resources)

            result = await session.read_resource("res://items/42")
            content = result.contents[0]
            assert isinstance(content, TextResourceContents)
            assert "item-42" in content.text


async def test_prompts(real_server_url):
    async with streamable_http_client(real_server_url) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            prompts = await session.list_prompts()
            assert {p.name for p in prompts.prompts} == {"greet"}

            result = await session.get_prompt("greet", {"language": "en"})
            content = result.messages[0].content
            assert isinstance(content, TextContent)
            assert "en speaker" in content.text


async def test_completion(real_server_url):
    async with streamable_http_client(real_server_url) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.complete(
                PromptReference(type="ref/prompt", name="greet"),
                {"name": "language", "value": "py"},
            )
            assert result.completion.values == ["python"]


async def test_a_subscription_reaches_the_real_sdk_client(real_server_url, registry):
    """Verify resource changes through the independent SDK client."""
    import asyncio

    from aiohttp_tiny_mcp.storage.hub import NOTIFICATIONS, topic

    heard: asyncio.Queue = asyncio.Queue()

    async def message_handler(message):
        if getattr(message, "method", None) == "notifications/resources/updated":
            await heard.put(message.params.uri)

    async with streamable_http_client(real_server_url) as (read, write):
        async with ClientSession(read, write, message_handler=message_handler) as session:
            result = await session.initialize()
            assert result.capabilities.resources.subscribe is True
            with pytest.warns(MCPDeprecationWarning, match="resources/subscribe"):
                await session.subscribe_resource("config://app")

            async def publish_until_heard() -> None:
                # The SDK opens its GET stream on its own schedule.
                while heard.empty():
                    await registry.hub.publish(
                        topic(NOTIFICATIONS),
                        {
                            "jsonrpc": "2.0",
                            "method": "notifications/resources/updated",
                            "params": {"uri": "config://app"},
                        },
                    )
                    await asyncio.sleep(0.05)

            publishing = asyncio.create_task(publish_until_heard())
            try:
                assert str(await asyncio.wait_for(heard.get(), 5)) == "config://app"
            finally:
                publishing.cancel()


async def test_logging_reaches_the_real_sdk_client(real_server_url):
    """`logging_callback` is where the SDK delivers `notifications/message`,
    and it only receives what the level allows."""
    import asyncio

    heard: asyncio.Queue = asyncio.Queue()

    async def logging_callback(params):
        await heard.put((params.level, params.data))

    async with streamable_http_client(real_server_url) as (read, write):
        async with ClientSession(read, write, logging_callback=logging_callback) as session:
            result = await session.initialize()
            assert result.capabilities.logging is not None
            with pytest.warns(MCPDeprecationWarning, match="logging"):
                await session.set_logging_level("warning")
            await session.call_tool("noisy", {})
            assert await asyncio.wait_for(heard.get(), 5) == ("warning", "odd")
            assert await asyncio.wait_for(heard.get(), 5) == ("error", "bad")
            assert heard.empty(), "debug was below the level and must not arrive"


async def test_the_old_transport_serves_the_official_sse_client(offered_over_sse):
    """`mcp.client.sse` is the SDK's client for the transport 2024-11-05
    introduced. It opens the stream, reads the `endpoint` event, and posts
    where that says -- an independent implementation of the half this package
    now serves.

    It asks for 2025-11-25, not 2024-11-05, and gets it. The transport is not
    the revision: it was defined by 2024-11-05 and the SDK speaks a newer one
    over it, so the handshake decides here as it does anywhere else.
    """
    from mcp.client.sse import sse_client

    async with sse_client(f"{offered_over_sse}/sse") as (read, write):
        async with ClientSession(read, write) as session:
            result = await session.initialize()
            tools = await session.list_tools()
            called = await session.call_tool("add", {"a": 2, "b": 3})

    assert result.protocol_version == "2025-11-25"
    assert "add" in {tool.name for tool in tools.tools}
    assert called.content[0].text == '{"result": 5}'
    assert called.structured_content == {"result": 5}
