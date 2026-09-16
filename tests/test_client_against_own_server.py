"""Client/server round trips across all revisions. SDK interoperability is tested separately."""

from __future__ import annotations

import asyncio

import pytest

from aiohttp_tiny_mcp import Client, elicit_accept, elicit_decline
from aiohttp_tiny_mcp.models import CallToolResult, TextContent, TextResourceContents
from aiohttp_tiny_mcp.protocol.selection import AdapterSet

pytestmark = pytest.mark.asyncio

ADAPTERS = AdapterSet.default().adapters


@pytest.mark.parametrize("adapter", ADAPTERS, ids=lambda a: a.version)
async def test_handshake_and_list_tools(real_server_url, adapter):
    async with Client(real_server_url, adapter) as client:
        result = await client.initialize()
        assert result["capabilities"]["tools"]["listChanged"] is True

        tools = await client.list_tools()
        assert {"add", "boom", "confirm", "counter"} <= {t.name for t in tools}


@pytest.mark.parametrize("adapter", ADAPTERS, ids=lambda a: a.version)
async def test_call_tool_success(real_server_url, adapter):
    async with Client(real_server_url, adapter) as client:
        await client.initialize()
        result = await client.call_tool("add", {"a": 2, "b": 3})
        assert isinstance(result, CallToolResult)
        assert result.is_error is False
        if adapter.version in ("2025-03-26", "2024-11-05"):
            assert result.structured_content is None
            assert result.content[0].text == '{"result": 5}'
        else:
            assert result.structured_content == {"result": 5}


async def test_modern_client_mirrors_declared_tool_parameters(real_server_url):
    adapter = AdapterSet.default().by_version["2026-07-28"]
    async with Client(real_server_url, adapter) as client:
        await client.initialize()
        await client.list_tools()
        result = await client.call_tool("routed", {"region": "日本語"})
        assert isinstance(result, CallToolResult)
        assert result.is_error is False


@pytest.mark.parametrize("adapter", ADAPTERS, ids=lambda a: a.version)
async def test_call_tool_bad_arguments_is_a_result_not_an_error(real_server_url, adapter):
    async with Client(real_server_url, adapter) as client:
        await client.initialize()
        result = await client.call_tool("add", {"a": "not-a-number"})
        assert isinstance(result, CallToolResult)
        assert result.is_error is True


@pytest.mark.parametrize("adapter", ADAPTERS, ids=lambda a: a.version)
async def test_call_tool_exception_is_a_result_not_an_error(real_server_url, adapter):
    async with Client(real_server_url, adapter) as client:
        await client.initialize()
        result = await client.call_tool("boom", {})
        assert isinstance(result, CallToolResult)
        assert result.is_error is True
        content = result.content[0]
        assert isinstance(content, TextContent)
        assert "kaboom" in content.text


@pytest.mark.parametrize("adapter", ADAPTERS, ids=lambda a: a.version)
async def test_resources(real_server_url, adapter):
    async with Client(real_server_url, adapter) as client:
        await client.initialize()
        resources = await client.list_resources()
        assert any(r.uri == "config://app" for r in resources)

        result = await client.read_resource("res://items/42")
        contents = result.contents[0]
        assert isinstance(contents, TextResourceContents)
        assert "item-42" in contents.text


@pytest.mark.parametrize("adapter", ADAPTERS, ids=lambda a: a.version)
async def test_prompts(real_server_url, adapter):
    async with Client(real_server_url, adapter) as client:
        await client.initialize()
        prompts = await client.list_prompts()
        assert {p.name for p in prompts} == {"greet"}

        result = await client.get_prompt("greet", {"language": "en"})
        content = result.messages[0].content
        assert isinstance(content, TextContent)
        assert "en speaker" in content.text


@pytest.mark.parametrize("adapter", ADAPTERS, ids=lambda a: a.version)
async def test_completion(real_server_url, adapter):
    async with Client(real_server_url, adapter) as client:
        await client.initialize()
        result = await client.complete(
            {"type": "ref/prompt", "name": "greet"}, {"name": "language", "value": "py"}
        )
        assert result.completion.values == ["python"]


@pytest.mark.parametrize("streaming", [False, True], ids=["json", "sse"])
async def test_mrtr_round_trip_on_2026_07_28(real_server_url, registry, streaming):
    registry.tools["confirm"].streaming = streaming
    adapter = AdapterSet.default().by_version["2026-07-28"]
    async with Client(real_server_url, adapter) as client:
        await client.initialize()

        first = await client.call_tool("confirm", {"service": "web"})
        assert isinstance(first, dict)
        assert first["resultType"] == "input_required"
        state = first["requestState"]

        second = await client.call_tool(
            "confirm",
            {"service": "web"},
            input_responses={"confirm": elicit_accept({"ok": True})},
            request_state=state,
        )
        assert isinstance(second, CallToolResult)
        content = second.content[0]
        assert isinstance(content, TextContent)
        assert "deployed web" in content.text


@pytest.mark.parametrize("adapter", ADAPTERS, ids=lambda a: a.version)
async def test_streaming_tool_result_survives_progress_notifications(real_server_url, adapter):
    """Progress may precede the result; it must not be returned as the reply."""
    async with Client(real_server_url, adapter) as client:
        await client.initialize()
        result = await client.call_tool("counter", {"a": 0, "b": 3})
        assert isinstance(result, CallToolResult)
        assert result.is_error is False
        content = result.content[0]
        assert isinstance(content, TextContent)
        assert "counted 3" in content.text


async def test_mrtr_unsupported_on_legacy(real_server_url):
    adapter = AdapterSet.default().by_version["2025-11-25"]
    async with Client(real_server_url, adapter) as client:
        await client.initialize()
        result = await client.call_tool("confirm", {"service": "x"})
        assert isinstance(result, CallToolResult)
        assert result.is_error is False
        content = result.content[0]
        assert isinstance(content, TextContent)
        assert "cannot ask" in content.text



ASKABLE = [a for a in ADAPTERS if a.can_ask or a.can_push_ask or a.asks_in_arguments]


def accepting(seen: list[str]):
    async def on_ask(request):
        seen.append(request["params"]["message"])
        return elicit_accept({"value": len(seen)})

    return on_ask


@pytest.mark.parametrize("adapter", ASKABLE, ids=lambda a: a.version)
async def test_one_question_is_answered_on_every_revision(real_server_url, adapter):
    seen: list[str] = []
    async with Client(real_server_url, adapter, on_ask=accepting(seen)) as client:
        await client.initialize()
        result = await client.call_tool("gated", {})
    assert seen == ["go ahead?"]
    assert isinstance(result, CallToolResult)
    content = result.content[0]
    assert isinstance(content, TextContent)
    assert content.text == "went ahead"


@pytest.mark.parametrize("adapter", ASKABLE, ids=lambda a: a.version)
async def test_two_questions_are_answered_on_every_revision(real_server_url, adapter):
    """MRTR must resend the first answer when the handler restarts for the second."""
    seen: list[str] = []
    async with Client(real_server_url, adapter, on_ask=accepting(seen)) as client:
        await client.initialize()
        result = await client.call_tool("twice", {})
    assert seen == ["first?", "second?"]
    assert isinstance(result, CallToolResult)
    content = result.content[0]
    assert isinstance(content, TextContent)
    assert content.text == "1+2"


@pytest.mark.parametrize("adapter", ASKABLE, ids=lambda a: a.version)
async def test_a_refusal_reaches_the_handler_on_every_revision(real_server_url, adapter):
    async def on_ask(request):
        return elicit_decline()

    async with Client(real_server_url, adapter, on_ask=on_ask) as client:
        await client.initialize()
        result = await client.call_tool("gated", {})
    assert isinstance(result, CallToolResult)
    content = result.content[0]
    assert isinstance(content, TextContent)
    assert content.text == "stopped at decline"


@pytest.mark.parametrize(
    "adapter", [a for a in ADAPTERS if a.can_push_ask], ids=lambda a: a.version
)
async def test_a_client_that_cannot_answer_is_not_asked(real_server_url, adapter):
    """Pushed questions need on_ask; MRTR questions can be returned for manual handling."""
    async with Client(real_server_url, adapter) as client:
        await client.initialize()
        result = await client.call_tool("defaulted", {})
    assert isinstance(result, CallToolResult)
    content = result.content[0]
    assert isinstance(content, TextContent)
    assert content.text == "defaulted to decline"


async def test_the_oldest_revision_asks_the_model_in_words(real_server_url):
    """Pre-elicitation clients receive retry instructions as text and machine-readable _meta."""
    adapter = AdapterSet.default().by_version["2025-03-26"]
    async with Client(real_server_url, adapter) as client:
        await client.initialize()
        asked = await client.call_tool("gated", {})
        assert isinstance(asked, dict)
        assert asked["isError"] is False
        assert "go ahead?" in asked["content"][0]["text"]
        assert "mcpAnswers" in asked["content"][0]["text"]

        requests, state = adapter.client_input_requests(asked)
        assert list(requests) == ["confirm"]
        answered = await client.call_tool(
            "gated", {}, input_responses={"confirm": elicit_accept({})}, request_state=state
        )
    assert isinstance(answered, CallToolResult)
    content = answered.content[0]
    assert isinstance(content, TextContent)
    assert content.text == "went ahead"


async def test_the_oldest_revision_does_not_widen_a_tool_that_cannot_ask(real_server_url):
    adapter = AdapterSet.default().by_version["2025-03-26"]
    async with Client(real_server_url, adapter) as client:
        await client.initialize()
        tools = {tool.name: tool for tool in await client.list_tools()}
    assert "mcpAnswers" in tools["gated"].input_schema["properties"]
    assert "mcpAnswers" not in tools["add"].input_schema["properties"]


@pytest.mark.parametrize("adapter", ADAPTERS, ids=lambda a: a.version)
async def test_one_listen_hears_a_change_on_every_revision(real_server_url, adapter, registry):
    async with Client(real_server_url, adapter) as client:
        await client.initialize()
        stream = client.listen(resources=["config://app"], tools_changed=True)
        heard = asyncio.create_task(anext_of(stream))
        keep_publishing = asyncio.create_task(publish_until_heard(registry.hub, heard))
        try:
            event = await asyncio.wait_for(asyncio.shield(heard), 5)
        finally:
            keep_publishing.cancel()
            await stream.aclose()
        assert event["method"] == "notifications/resources/updated"
        assert event["params"]["uri"] == "config://app"


async def anext_of(stream):
    async for frame in stream:
        if frame["method"] != "notifications/subscriptions/acknowledged":
            return frame
    raise AssertionError("stream ended before it said anything")


async def publish_until_heard(hub, heard):
    """Publish until the subscription receives an event, avoiding assumptions about connection
    timing.
    """
    from aiohttp_tiny_mcp.hub import NOTIFICATIONS, topic

    while not heard.done():
        await hub.publish(
            topic(NOTIFICATIONS),
            {
                "jsonrpc": "2.0",
                "method": "notifications/resources/updated",
                "params": {"uri": "config://app"},
            },
        )
        await asyncio.sleep(0.02)
