"""Input round trips across endpoints sharing state storage."""

from __future__ import annotations

import pytest
from aiohttp.test_utils import TestClient, TestServer

from aiohttp_tiny_mcp import Client, ClientError, Endpoint, elicit_accept
from aiohttp_tiny_mcp.protocol.models import CallToolResult, TextContent
from aiohttp_tiny_mcp.protocol.selection import AdapterSet

pytestmark = pytest.mark.asyncio

MODERN = AdapterSet.default().by_version["2026-07-28"]
OLDEST = AdapterSet.default().by_version["2025-03-26"]


async def test_state_left_on_one_node_is_read_on_another(registry):
    async with (
        TestServer(Endpoint(registry).app()) as first,
        TestServer(Endpoint(registry).app()) as second,
    ):
        async with Client(str(first.make_url("/mcp")), MODERN) as client:
            asked = await client.call_tool("confirm", {"service": "web"})
        assert isinstance(asked, dict)
        state = asked["requestState"]

        async with Client(str(second.make_url("/mcp")), MODERN) as client:
            done = await client.call_tool(
                "confirm",
                {"service": "web"},
                input_responses={"confirm": elicit_accept({"ok": True})},
                request_state=state,
            )
    assert isinstance(done, CallToolResult)
    content = done.content[0]
    assert isinstance(content, TextContent)
    assert content.text == "deployed web"


async def test_the_oldest_revision_resumes_across_nodes_too(registry):
    """Tool-argument state ids use the same shared store as MRTR."""
    async with (
        TestServer(Endpoint(registry).app()) as first,
        TestServer(Endpoint(registry).app()) as second,
    ):
        async with Client(str(first.make_url("/mcp")), OLDEST) as client:
            asked = await client.call_tool("confirm", {"service": "web"})
            requests, state = OLDEST.client_input_requests(asked)
            assert list(requests) == ["confirm"]

        async with Client(str(second.make_url("/mcp")), OLDEST) as client:
            done = await client.call_tool(
                "confirm",
                {"service": "web"},
                input_responses={"confirm": elicit_accept({"ok": True})},
                request_state=state,
            )
    assert isinstance(done, CallToolResult)
    content = done.content[0]
    assert isinstance(content, TextContent)
    assert content.text == "deployed web"


async def test_state_from_one_call_does_not_resume_another(registry):
    """State for deploy web must not authorize deploy db."""
    async with TestClient(TestServer(Endpoint(registry).app())) as http:
        async with Client(str(http.make_url("/mcp")), MODERN) as client:
            asked = await client.call_tool("confirm", {"service": "web"})
            state = asked["requestState"]
            with pytest.raises(ClientError, match="requestState"):
                await client.call_tool(
                    "confirm",
                    {"service": "db"},
                    input_responses={"confirm": elicit_accept({"ok": True})},
                    request_state=state,
                )


async def test_state_is_spent_once_the_round_trip_ends(registry):
    async with TestClient(TestServer(Endpoint(registry).app())) as http:
        async with Client(str(http.make_url("/mcp")), MODERN) as client:
            asked = await client.call_tool("confirm", {"service": "web"})
            state = asked["requestState"]
            answered = {"confirm": elicit_accept({"ok": True})}
            first = await client.call_tool(
                "confirm", {"service": "web"}, input_responses=answered, request_state=state
            )
            assert isinstance(first, CallToolResult)
            with pytest.raises(ClientError, match="requestState"):
                await client.call_tool(
                    "confirm", {"service": "web"}, input_responses=answered, request_state=state
                )
