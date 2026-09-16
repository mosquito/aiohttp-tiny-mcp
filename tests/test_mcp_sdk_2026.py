"""Server interoperability with the official 2026-07-28 SDK client, including automatic MRTR
resolution.
"""

from __future__ import annotations

import pytest
from mcp import Client
from mcp_types import ElicitResult

pytestmark = pytest.mark.asyncio


async def test_initialize(real_server_url):
    """Use mode=auto to exercise server/discover; an explicit modern mode synthesizes discovery
    locally.
    """
    async with Client(real_server_url, mode="auto") as client:
        assert client.server_info.name == "demo"
        assert client.protocol_version == "2026-07-28"


async def test_list_and_call_tool(real_server_url):
    async with Client(real_server_url, mode="2026-07-28") as client:
        tools = await client.list_tools()
        names = {t.name for t in tools.tools}
        assert {"add", "boom", "confirm", "counter"} <= names

        result = await client.call_tool("add", {"a": 2, "b": 3})
        assert result.is_error is False
        assert result.structured_content == {"result": 5}
        assert result.result_type == "complete"


async def test_call_tool_bad_arguments_is_a_result_not_an_error(real_server_url):
    async with Client(real_server_url, mode="2026-07-28") as client:
        result = await client.call_tool("add", {"a": "not-a-number"})
        assert result.is_error is True


async def test_non_object_structured_content_unrestricted(real_server_url):
    async with Client(real_server_url, mode="2026-07-28") as client:
        result = await client.call_tool("listy", {})
        assert result.structured_content == [1, 2, 3]


async def test_resources(real_server_url):
    async with Client(real_server_url, mode="2026-07-28") as client:
        resources = await client.list_resources()
        assert any(r.uri == "config://app" for r in resources.resources)

        result = await client.read_resource("res://items/42")
        content = result.contents[0]
        assert "item-42" in content.text


async def test_prompts(real_server_url):
    async with Client(real_server_url, mode="2026-07-28") as client:
        prompts = await client.list_prompts()
        assert {p.name for p in prompts.prompts} == {"greet"}

        result = await client.get_prompt("greet", {"language": "en"})
        content = result.messages[0].content
        assert "en speaker" in content.text


async def test_completion(real_server_url):
    async with Client(real_server_url, mode="2026-07-28") as client:
        result = await client.complete(
            {"type": "ref/prompt", "name": "greet"}, {"name": "language", "value": "py"}
        )
        assert result.completion.values == ["python"]


async def test_mrtr_accept(real_server_url):
    async def elicitation_callback(context, params):
        return ElicitResult(action="accept", content={"ok": True})

    async with Client(
        real_server_url, mode="2026-07-28", elicitation_callback=elicitation_callback
    ) as client:
        result = await client.call_tool("confirm", {"service": "web"})
        assert result.is_error is False
        assert "deployed web" in result.content[0].text


async def test_mrtr_decline(real_server_url):
    async def elicitation_callback(context, params):
        return ElicitResult(action="decline")

    async with Client(
        real_server_url, mode="2026-07-28", elicitation_callback=elicitation_callback
    ) as client:
        result = await client.call_tool("confirm", {"service": "web"})
        assert "cancelled" in result.content[0].text
