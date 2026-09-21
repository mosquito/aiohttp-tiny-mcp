"""What the client accepts from a server it does not control."""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from aiohttp_tiny_mcp import (
    BlobResourceContents,
    CallToolResult,
    Client,
    EmbeddedResource,
    Registry,
    TextResourceContents,
)
from aiohttp_tiny_mcp.protocol.models import ListToolsResult
from aiohttp_tiny_mcp.protocol.selection import AdapterSet
from aiohttp_tiny_mcp.testing import serving

ADAPTERS = AdapterSet.default().adapters


class Nothing(BaseModel):
    pass


def test_tool_without_a_description_is_still_a_tool() -> None:
    """A server may omit the description; the rest of the list must survive it."""
    result = ListToolsResult.model_validate(
        {"tools": [{"name": "add", "inputSchema": {"type": "object"}}]}
    )
    assert result.tools[0].name == "add"
    assert result.tools[0].description == ""


def test_embedded_text_resource_is_a_content_block() -> None:
    result = CallToolResult.model_validate(
        {"content": [{"type": "resource", "resource": {"uri": "file:///a.txt", "text": "hello"}}]}
    )
    block = result.content[0]
    assert isinstance(block, EmbeddedResource)
    assert isinstance(block.resource, TextResourceContents)
    assert block.resource.uri == "file:///a.txt"
    assert block.resource.text == "hello"


def test_embedded_blob_resource_is_a_content_block() -> None:
    result = CallToolResult.model_validate(
        {
            "content": [
                {
                    "type": "resource",
                    "resource": {"uri": "file:///a.bin", "blob": "AQID", "mimeType": "image/png"},
                }
            ]
        }
    )
    block = result.content[0]
    assert isinstance(block, EmbeddedResource)
    assert isinstance(block.resource, BlobResourceContents)
    assert block.resource.blob == "AQID"
    assert block.resource.mime_type == "image/png"


@pytest.mark.parametrize("adapter", ADAPTERS, ids=lambda a: a.version)
async def test_embedded_resource_survives_a_round_trip(adapter) -> None:
    """Every revision carries an embedded resource; only resource links are newer."""
    registry = Registry("demo", "1.0")

    @registry.tool
    async def report(args: Nothing) -> CallToolResult:
        """Return a file as content, not as a link."""
        return CallToolResult(
            content=[
                EmbeddedResource(
                    resource=TextResourceContents(uri="file:///report.txt", text="done")
                )
            ]
        )

    async with serving(registry) as url:
        async with Client(url, adapter) as client:
            await client.initialize()
            result = await client.call_tool("report", {})

    assert isinstance(result, CallToolResult)
    block = result.content[0]
    assert isinstance(block, EmbeddedResource)
    assert isinstance(block.resource, TextResourceContents)
    assert block.resource.text == "done"
