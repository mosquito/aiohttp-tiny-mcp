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

from aiohttp_tiny_mcp import Client
from aiohttp_tiny_mcp.protocol.selection import AdapterSet

pytestmark = pytest.mark.asyncio

OLDEST = AdapterSet.default().by_version["2025-03-26"]
NEXT = AdapterSet.default().by_version["2025-06-18"]


class Nothing(BaseModel):
    pass


async def listed(url: str, adapter):
    async with Client(url, adapter) as client:
        await client.initialize()
        return {tool.name: tool for tool in await client.list_tools()}


async def test_a_tool_carries_no_output_schema(projection_registry, real_endpoint):
    url = await real_endpoint(projection_registry)
    oldest = await listed(url, OLDEST)
    newer = await listed(url, NEXT)

    assert oldest["counted"].output_schema is None
    assert newer["counted"].output_schema is not None


async def test_a_tool_carries_no_title(projection_registry, real_endpoint):
    url = await real_endpoint(projection_registry)
    oldest = await listed(url, OLDEST)
    newer = await listed(url, NEXT)

    assert oldest["described"].title is None
    assert newer["described"].title == "A Display Title"


async def test_a_resource_carries_no_title(projection_registry, real_endpoint):
    url = await real_endpoint(projection_registry)
    async with Client(url, OLDEST) as client:
        await client.initialize()
        oldest = await client.list_resources()
    async with Client(url, NEXT) as client:
        await client.initialize()
        newer = await client.list_resources()

    assert oldest[0].title is None
    assert newer[0].title == "Configuration"


async def test_a_result_carries_no_structured_content(projection_registry, real_endpoint):
    url = await real_endpoint(projection_registry)
    async with Client(url, OLDEST) as client:
        await client.initialize()
        old = await client.call_tool("counted", {})
    async with Client(url, NEXT) as client:
        await client.initialize()
        new = await client.call_tool("counted", {})

    assert old.structured_content is None
    assert new.structured_content == {"total": 5}
    assert "5" in old.content[0].text


async def test_a_resource_link_is_dropped(projection_registry, real_endpoint):
    url = await real_endpoint(projection_registry)
    async with Client(url, OLDEST) as client:
        await client.initialize()
        old = await client.call_tool("linking", {})
    async with Client(url, NEXT) as client:
        await client.initialize()
        new = await client.call_tool("linking", {})

    assert [type(block).__name__ for block in old.content] == ["TextContent"]
    assert [type(block).__name__ for block in new.content] == ["TextContent", "ResourceLink"]
