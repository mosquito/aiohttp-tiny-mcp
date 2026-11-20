"""The server and client an example is written against.

What these check is that the shortcut is not a stub: a client from `connect`
goes through the same encoding, dispatch and decoding as one on a socket, and
sees the same differences between revisions. A helper that quietly answered
from a dictionary would make every example that uses it worthless.
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from aiohttp_tiny_mcp import Exchange, Registry, elicit
from aiohttp_tiny_mcp.testing import connect, every_revision, over_http, pick, serving

pytestmark = pytest.mark.asyncio


class Add(BaseModel):
    a: int
    b: int


class Sum(BaseModel):
    result: int


class Deploy(BaseModel):
    service: str


class Nothing(BaseModel):
    pass


def demo() -> Registry:
    registry = Registry("demo", "1.0")

    @registry.tool
    async def add(args: Add) -> Sum:
        """Add two integers."""
        return Sum(result=args.a + args.b)

    @registry.tool
    async def deploy(args: Deploy, ex: Exchange) -> str:
        """Deploy a service, once somebody agrees to it."""
        agreed = await ex.ask("confirm", elicit(f"Deploy {args.service}?"))
        return f"deployed {args.service}" if agreed.accepted else f"stopped at {agreed.action}"

    @registry.resource("config://app", mime_type="application/json")
    async def config(args: Nothing) -> dict:
        """Fixed configuration."""
        return {"debug": False}

    return registry


async def test_a_tool_answers_through_memory():
    async with connect(demo()) as client:
        result = await client.call_tool("add", {"a": 2, "b": 3})
    assert result.structured_content == {"result": 5}


async def test_bad_arguments_still_fail_the_way_they_would():
    """Validation runs where it always runs. A helper that skipped it would
    make every example asserting on a failure a lie."""
    async with connect(demo()) as client:
        result = await client.call_tool("add", {"a": "two"})
    assert result.is_error is True


async def test_the_same_call_over_a_socket_says_the_same_thing():
    async with over_http(demo()) as client:
        result = await client.call_tool("add", {"a": 2, "b": 3})
    assert result.structured_content == {"result": 5}


async def test_resources_and_prompts_come_through_too():
    async with connect(demo()) as client:
        listed = await client.list_resources()
        read = await client.read_resource("config://app")
    assert any(item.uri == "config://app" for item in listed)
    assert "debug" in read.contents[0].text


async def test_every_revision_answers():
    seen = await every_revision(demo(), lambda client: client.call_tool("add", {"a": 2, "b": 3}))
    assert set(seen) == {
        "2026-07-28",
        "2025-11-25",
        "2025-06-18",
        "2025-03-26",
        "2024-11-05",
    }
    assert all(got.structured_content == {"result": 5} for got in seen.values())


async def test_a_revision_is_not_flattened_into_the_newest():
    """Only `2026-07-28` carries a result that is not an object. The older
    ones send the same value as text and have nowhere to put it structured.
    A helper that hid that would hide the thing this package is for."""
    registry = Registry("demo", "1.0")

    @registry.tool
    async def count(args: Nothing) -> int:
        """How many there are."""
        return 7

    seen = await every_revision(registry, lambda client: client.call_tool("count", {}))
    assert seen["2026-07-28"].structured_content == 7
    assert seen["2025-11-25"].structured_content is None
    assert all(got.content[0].text == "7" for got in seen.values())


async def test_a_revision_is_chosen_by_name():
    async with connect(demo(), adapter="2025-06-18") as client:
        result = await client.call_tool("add", {"a": 1, "b": 1})
    assert result.structured_content == {"result": 2}


async def test_the_newest_revision_is_the_default():
    assert pick(None).version == "2026-07-28"


async def test_a_question_is_answered_from_a_mapping():
    async with connect(demo(), answers={"Deploy": {}}) as client:
        result = await client.call_tool("deploy", {"service": "web"})
    assert result.content[0].text == "deployed web"


async def test_none_declines():
    async with connect(demo(), answers={"Deploy": None}) as client:
        result = await client.call_tool("deploy", {"service": "web"})
    assert result.content[0].text == "stopped at decline"


async def test_a_question_nobody_answered_is_declined():
    """A mapping that does not match is a decline, not a hang: an example
    that forgot an answer must fail rather than stop."""
    async with connect(demo(), answers={"something else": {}}) as client:
        result = await client.call_tool("deploy", {"service": "web"})
    assert result.content[0].text == "stopped at decline"


async def test_a_question_is_answered_on_every_revision():
    """Including the two that have no elicitation of their own, which this
    package carries through the tool call itself."""
    seen = await every_revision(
        demo(),
        lambda client: client.call_tool("deploy", {"service": "web"}),
        answers={"Deploy": {}},
    )
    assert {version: got.content[0].text for version, got in seen.items()} == {
        version: "deployed web" for version in seen
    }


async def test_serving_gives_a_url_that_answers():
    import aiohttp

    async with serving(demo()) as url:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                url,
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json, text/event-stream",
                    "MCP-Protocol-Version": "2025-11-25",
                },
            ) as answered:
                assert answered.status == 200
                body = await answered.json()
    assert {tool["name"] for tool in body["result"]["tools"]} == {"add", "deploy"}


async def test_a_header_reaches_the_server():
    """What `over_http` is for: the transport carrying something the
    in-memory pair has nowhere to put."""
    seen: list[str | None] = []
    registry = Registry("demo", "1.0")

    @registry.tool
    async def peek(args: Nothing, ex: Exchange) -> str:
        """Report one request header."""
        seen.append(ex.request.headers.get("X-Trace"))
        return "looked"

    async with over_http(registry, headers={"X-Trace": "abc"}) as client:
        await client.call_tool("peek", {})
    assert seen == ["abc"]


async def test_nothing_is_left_running():
    """Both helpers clean up, so an example may use them in a loop."""
    for _ in range(3):
        async with connect(demo()) as client:
            await client.call_tool("add", {"a": 1, "b": 1})
        async with serving(demo()) as url:
            assert url.startswith("http://127.0.0.1:")
