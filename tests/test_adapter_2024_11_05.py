"""The first published revision, served from the same handlers as the rest.

Everything it has, 2025-03-26 also has, so the adapter is that one minus what
arrived later. Checked against the published pages for both revisions: a tool
here carries `name`, `description` and `inputSchema` and nothing else, a result
carries text, image or embedded resource content, and JSON-RPC batching does
not exist yet.

What it does not get is the HTTP+SSE transport a real client of the revision
speaks over HTTP. Over stdio, and over Streamable HTTP for anything that
chooses to use it, the adapter is the whole of the revision.
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from aiohttp_tiny_mcp import Client, Exchange, MemoryHub, MemorySessionStore, Registry, elicit
from aiohttp_tiny_mcp.protocol.models import AudioContent, CallToolResult, TextContent
from aiohttp_tiny_mcp.protocol.selection import AdapterSet

pytestmark = pytest.mark.asyncio

FIRST = AdapterSet.default().by_version["2024-11-05"]
NEXT = AdapterSet.default().by_version["2025-03-26"]


class Nothing(BaseModel):
    pass


async def careful(args: Nothing) -> str:
    """Declares hints this revision has no field for."""
    return "done"


async def speaking(args: Nothing) -> CallToolResult:
    """Returns audio, which arrived in 2025-03-26."""
    return CallToolResult(
        content=[TextContent(text="listen"), AudioContent(data="AAA=", mime_type="audio/wav")]
    )


async def asking(args: Nothing, ex: Exchange) -> str:
    """Asks, on a revision that predates elicitation by two releases."""
    agreed = await ex.ask("confirm", elicit("Go ahead?"))
    return "went ahead" if agreed.accepted else f"stopped at {agreed.action.value}"


@pytest.fixture
def offered() -> Registry:
    registry = Registry("p", "1.0", hub=MemoryHub(), session_store=MemorySessionStore())
    registry.tool(careful, annotations={"readOnlyHint": True, "destructiveHint": False})
    registry.tool(speaking)
    registry.tool(asking)
    return registry


async def listed(url: str, adapter):
    async with Client(url, adapter) as client:
        await client.initialize()
        return {tool.name: tool for tool in await client.list_tools()}


async def test_the_handshake_answers_with_the_revision_that_was_asked_for(offered, real_endpoint):
    """Echoing a newer revision would tell an old client to speak something it
    does not know."""
    url = await real_endpoint(offered)
    async with Client(url, FIRST) as client:
        result = await client.initialize()
    assert result["protocolVersion"] == "2024-11-05"


async def test_a_tool_carries_only_the_three_fields_this_revision_defines(offered, real_endpoint):
    url = await real_endpoint(offered)
    first = await listed(url, FIRST)
    later = await listed(url, NEXT)

    assert first["careful"].annotations is None
    assert first["careful"].output_schema is None
    assert first["careful"].title is None
    assert later["careful"].annotations == {"readOnlyHint": True, "destructiveHint": False}


async def test_audio_content_is_dropped(offered, real_endpoint):
    url = await real_endpoint(offered)
    async with Client(url, FIRST) as client:
        await client.initialize()
        first = await client.call_tool("speaking", {})
    async with Client(url, NEXT) as client:
        await client.initialize()
        later = await client.call_tool("speaking", {})

    assert [type(block).__name__ for block in first.content] == ["TextContent"]
    assert [type(block).__name__ for block in later.content] == ["TextContent", "AudioContent"]


async def test_batching_is_refused(offered, real_endpoint):
    """JSON-RPC batch arrays arrived in 2025-03-26."""
    import aiohttp

    url = await real_endpoint(offered)
    batch = [{"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {}}]
    async with aiohttp.ClientSession() as http:
        async with http.post(f"{url}?mcp=2024-11-05", json=batch) as response:
            refused = await response.json()
        async with http.post(f"{url}?mcp=2025-03-26", json=batch) as response:
            accepted = await response.json()

    assert refused["error"]["code"] == -32600
    assert isinstance(accepted, list) and accepted[0]["result"] == {}


async def test_a_progress_message_is_not_sent(offered, real_endpoint):
    """The `message` field on a progress notification arrived in 2025-03-26."""
    registry = Registry("p", "1.0", hub=MemoryHub(), session_store=MemorySessionStore())

    @registry.tool(streaming=True)
    async def working(args: Nothing, ex: Exchange) -> str:
        """Reports progress with a message."""
        await ex.progress(1, 2, message="halfway")
        return "done"

    url = await real_endpoint(registry)
    seen: list[dict] = []

    async def note(frame):
        if frame.get("method") == "notifications/progress":
            seen.append(frame["params"])

    async with Client(url, FIRST, on_notification=note) as client:
        await client.initialize()
        await client.call_tool("working", {})
    first = list(seen)
    seen.clear()
    async with Client(url, NEXT, on_notification=note) as client:
        await client.initialize()
        await client.call_tool("working", {})

    assert first and "message" not in first[0]
    assert seen and seen[0]["message"] == "halfway"


async def test_it_can_still_be_asked(offered, real_endpoint):
    """Elicitation arrived in 2025-06-18, so this revision has no mechanism.
    The convention that covers 2025-03-26 covers this one too."""
    from aiohttp_tiny_mcp import elicit_accept

    async def answer(request):
        return elicit_accept({})

    url = await real_endpoint(offered)
    async with Client(url, FIRST, on_ask=answer) as client:
        await client.initialize()
        result = await client.call_tool("asking", {})

    assert result.content[0].text == "went ahead"
