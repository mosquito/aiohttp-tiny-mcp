"""Asking the client's model to complete something, on every revision that carries it."""

from __future__ import annotations

import asyncio

import pytest
from pydantic import BaseModel

from aiohttp_tiny_mcp import (
    CompletionMessage,
    CompletionRequest,
    Exchange,
    MemoryHub,
    MemorySessionStore,
    NeedInput,
    Registry,
    TextContent,
    elicit,
)
from aiohttp_tiny_mcp.client.base import ClientError
from aiohttp_tiny_mcp.protocol.selection import AdapterSet
from aiohttp_tiny_mcp.testing import connect, over_http

# `asyncio_mode = auto` runs the async tests here.

VERSIONS = tuple(AdapterSet.default().by_version)


class Topic(BaseModel):
    about: str


def registry_that_samples() -> Registry:
    registry = Registry("sampling", "1.0", hub=MemoryHub(), session_store=MemorySessionStore())

    @registry.tool
    async def summarize(args: Topic, ex: Exchange) -> str:
        """Have the client's model write one sentence."""
        answered = await ex.completion(
            messages=[
                CompletionMessage(role="user", content=TextContent(text=f"About {args.about}"))
            ],
            max_tokens=64,
            system_prompt="Answer in one sentence.",
        )
        return f"{answered.model}: {answered.text}"

    @registry.tool
    async def twice(args: Topic, ex: Exchange) -> str:
        """Two completions in one handler, under their own keys."""
        first = await ex.completion(
            messages=[{"role": "user", "content": {"type": "text", "text": "one"}}],
            key="first",
            max_tokens=8,
        )
        second = await ex.completion(
            messages=[{"role": "user", "content": {"type": "text", "text": "two"}}],
            key="second",
            max_tokens=8,
        )
        return f"{first.text}/{second.text}"

    return registry


@pytest.mark.parametrize("version", VERSIONS)
@pytest.mark.parametrize("transport", [connect, over_http], ids=["stdio", "http"])
async def test_a_handler_samples_on_every_revision(version, transport):
    """Pushed where the revision has a stream, carried in the result where it has MRTR."""
    async with transport(
        registry_that_samples(), adapter=version, completion="a summary"
    ) as client:
        answered = await client.call_tool("summarize", {"about": "trees"})
    assert answered.content[0].text == "test-model: a summary"


@pytest.mark.parametrize("version", VERSIONS)
@pytest.mark.parametrize("transport", [connect, over_http], ids=["stdio", "http"])
async def test_a_client_that_offers_no_model_is_refused(version, transport):
    """The refusal names the capability the client would have to declare."""
    async with transport(registry_that_samples(), adapter=version) as client:
        with pytest.raises(ClientError, match="sampling") as refused:
            await client.call_tool("summarize", {"about": "trees"})
    assert refused.value.data == {"requiredCapabilities": {"sampling": {}}}


@pytest.mark.parametrize("version", ["2026-07-28", "2025-11-25"])
async def test_two_completions_keep_their_own_keys(version):
    """Each request is answered under its own key, so one handler may sample twice."""
    answers = {"one": "first text", "two": "second text"}

    async def on_completion(request):
        asked = request["params"]["messages"][0]["content"]["text"]
        return {
            "role": "assistant",
            "content": {"type": "text", "text": answers[asked]},
            "model": "m",
        }

    async with connect(
        registry_that_samples(), adapter=version, on_completion=on_completion
    ) as client:
        answered = await client.call_tool("twice", {"about": "x"})
    assert answered.content[0].text == "first text/second text"


async def test_a_refusal_reaches_the_handler_as_a_failure():
    """A completion that did not happen is not an empty completion."""

    async def refusing(request):
        return {"code": -32000, "message": "no model available"}

    async with connect(
        registry_that_samples(), adapter="2026-07-28", on_completion=refusing
    ) as client:
        with pytest.raises(ClientError, match="refused the completion"):
            await client.call_tool("summarize", {"about": "x"})


async def test_an_answer_of_another_shape_is_a_failure():
    """A reply that is not a completion fails rather than reaching a handler."""

    async def wrong(request):
        return {"text": "no role, no model"}

    async with connect(
        registry_that_samples(), adapter="2026-07-28", on_completion=wrong
    ) as client:
        with pytest.raises(ClientError, match="answered sampling/createMessage"):
            await client.call_tool("summarize", {"about": "x"})


async def test_the_request_carries_what_the_handler_stated():
    """The wire request is the specification's shape, including the optional fields."""
    seen = []

    async def recording(request):
        seen.append(request)
        return {"role": "assistant", "content": {"type": "text", "text": "ok"}, "model": "m"}

    async with over_http(
        registry_that_samples(), adapter="2025-11-25", on_completion=recording
    ) as client:
        await client.call_tool("summarize", {"about": "trees"})

    assert seen[0]["method"] == "sampling/createMessage"
    params = seen[0]["params"]
    assert params["maxTokens"] == 64
    assert params["systemPrompt"] == "Answer in one sentence."
    assert params["messages"] == [
        {"role": "user", "content": {"type": "text", "text": "About trees"}}
    ]


async def test_a_client_declares_sampling_only_with_a_callback():
    """Silence is not a promise: the capability follows the callback."""
    async with connect(registry_that_samples(), adapter="2026-07-28") as client:
        assert "sampling" not in client.capabilities
    async with connect(registry_that_samples(), adapter="2026-07-28", completion="x") as client:
        assert client.capabilities["sampling"] == {}


@pytest.mark.parametrize("version", VERSIONS)
@pytest.mark.parametrize("transport", [connect, over_http], ids=["stdio", "http"])
@pytest.mark.parametrize("reply", [{"code": -32000, "message": "refused"}, {"text": "invalid"}, {}])
async def test_a_restarted_completion_returns_one_protocol_failure(version, transport, reply):
    registry = Registry("restarted", "1.0")

    @registry.tool
    async def resumed(args: Topic, ex: Exchange) -> str:
        answered = ex.completed()
        if answered is None:
            raise NeedInput({"completion": CompletionRequest(messages=[], max_tokens=8).as_input()})
        return answered.text

    calls = []

    async def answering(request):
        calls.append(request)
        return reply

    async with transport(registry, adapter=version, on_completion=answering) as client:
        with pytest.raises(ClientError) as failed:
            await asyncio.wait_for(client.call_tool("resumed", {"about": "x"}), 2)
    assert failed.value.code == -32600
    assert len(calls) == 1


@pytest.mark.parametrize("version", VERSIONS)
@pytest.mark.parametrize("transport", [connect, over_http], ids=["stdio", "http"])
async def test_each_input_round_requires_the_capability_it_uses(version, transport):
    registry = Registry("rounds", "1.0")

    @registry.tool
    async def confirmed(args: Topic, ex: Exchange) -> str:
        if not ex.answered("confirm"):
            raise NeedInput({"confirm": elicit("Proceed?")})
        return (await ex.completion(messages=[], max_tokens=8)).text

    asked = []

    async def confirm(request):
        asked.append(request["method"])
        return {"action": "accept", "content": {}}

    async with transport(registry, adapter=version, on_ask=confirm) as client:
        with pytest.raises(ClientError) as failed:
            await asyncio.wait_for(client.call_tool("confirmed", {"about": "x"}), 2)
    assert failed.value.data == {"requiredCapabilities": {"sampling": {}}}
    assert asked == ["elicitation/create"]


class Lease:
    open = True


@pytest.mark.parametrize("version", VERSIONS)
async def test_a_restarted_handler_does_not_reuse_a_closed_dependency(version):
    registry = Registry("dependencies", "1.0")
    leases = []

    async def lease(ex):
        value = Lease()
        leases.append(value)
        try:
            yield value
        finally:
            value.open = False

    registry.provide(Lease, lease)

    @registry.tool
    async def resumed(args: Topic, ex: Exchange, value: Lease) -> str:
        assert value.open
        answered = ex.completed()
        if answered is None:
            raise NeedInput({"completion": CompletionRequest(messages=[], max_tokens=8).as_input()})
        return answered.text

    async with connect(registry, adapter=version, completion="ok") as client:
        result = await client.call_tool("resumed", {"about": "x"})
    assert result.content[0].text == "ok"
    assert len(leases) == 2
    assert all(not value.open for value in leases)
