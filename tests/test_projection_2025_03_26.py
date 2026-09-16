"""What 2025-03-26 has no field for.

Checked against the published specification for that revision: a tool carries
`name`, `description`, `inputSchema` and `annotations`, and a result carries
`content` and `isError`. `outputSchema`, `structuredContent`, `title` and
`resource_link` all arrived in 2025-06-18.

Sending a field a revision never defined is not harmless. A strict client
rejects the message; a lenient one shows the model a schema it was never told
how to read.
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from aiohttp_tiny_mcp import Client, MemoryHub, MemorySessionStore, Registry
from aiohttp_tiny_mcp.models import CallToolResult, ResourceLink, TextContent
from aiohttp_tiny_mcp.protocol.selection import AdapterSet

pytestmark = pytest.mark.asyncio

OLDEST = AdapterSet.default().by_version["2025-03-26"]
NEXT = AdapterSet.default().by_version["2025-06-18"]


class Nothing(BaseModel):
    pass


class Sum(BaseModel):
    total: int


async def counted(args: Nothing) -> Sum:
    """Returns a model, so it has an output schema and structured content."""
    return Sum(total=5)


async def linking(args: Nothing) -> CallToolResult:
    """Returns a link, which is a kind of content 2025-06-18 introduced."""
    return CallToolResult(
        content=[TextContent(text="see this"), ResourceLink(uri="config://app", name="config")]
    )


async def described(args: Nothing) -> str:
    """Has a display title."""
    return "ok"


@pytest.fixture
def offered() -> Registry:
    registry = Registry("p", "1.0", hub=MemoryHub(), session_store=MemorySessionStore())
    registry.tool(counted)
    registry.tool(linking)
    registry.tool(described, title="A Display Title")
    registry.resource("config://app", counted, title="Configuration")
    return registry


async def listed(url: str, adapter):
    async with Client(url, adapter) as client:
        await client.initialize()
        return {tool.name: tool for tool in await client.list_tools()}


async def test_a_tool_carries_no_output_schema(offered, real_endpoint):
    url = await real_endpoint(offered)
    oldest = await listed(url, OLDEST)
    newer = await listed(url, NEXT)

    assert oldest["counted"].output_schema is None
    assert newer["counted"].output_schema is not None


async def test_a_tool_carries_no_title(offered, real_endpoint):
    url = await real_endpoint(offered)
    oldest = await listed(url, OLDEST)
    newer = await listed(url, NEXT)

    assert oldest["described"].title is None
    assert newer["described"].title == "A Display Title"


async def test_a_resource_carries_no_title(offered, real_endpoint):
    url = await real_endpoint(offered)
    async with Client(url, OLDEST) as client:
        await client.initialize()
        oldest = await client.list_resources()
    async with Client(url, NEXT) as client:
        await client.initialize()
        newer = await client.list_resources()

    assert oldest[0].title is None
    assert newer[0].title == "Configuration"


async def test_a_result_carries_no_structured_content(offered, real_endpoint):
    url = await real_endpoint(offered)
    async with Client(url, OLDEST) as client:
        await client.initialize()
        old = await client.call_tool("counted", {})
    async with Client(url, NEXT) as client:
        await client.initialize()
        new = await client.call_tool("counted", {})

    assert old.structured_content is None
    assert new.structured_content == {"total": 5}
    assert "5" in old.content[0].text


async def test_a_resource_link_is_dropped(offered, real_endpoint):
    url = await real_endpoint(offered)
    async with Client(url, OLDEST) as client:
        await client.initialize()
        old = await client.call_tool("linking", {})
    async with Client(url, NEXT) as client:
        await client.initialize()
        new = await client.call_tool("linking", {})

    assert [type(block).__name__ for block in old.content] == ["TextContent"]
    assert [type(block).__name__ for block in new.content] == ["TextContent", "ResourceLink"]
