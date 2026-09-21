"""Exercise serve_stdio with in-memory reader/writer fixtures."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from typing import Any

import pytest

from aiohttp_tiny_mcp.protocol.selection import AdapterSet
from aiohttp_tiny_mcp.server.stdio import serve_stdio

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(10)]


def feed(*messages: dict[str, Any]) -> asyncio.StreamReader:
    reader = asyncio.StreamReader()
    for msg in messages:
        reader.feed_data((json.dumps(msg) + "\n").encode())
    reader.feed_eof()
    return reader


async def test_initialize_and_call_tool(registry):
    outputs: list[Mapping[str, Any]] = []
    reader = feed(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2025-11-25"},
        },
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "add", "arguments": {"a": 2, "b": 3}},
        },
    )
    await serve_stdio(registry, reader, outputs.append)

    assert outputs[0]["result"]["protocolVersion"] == "2025-11-25"
    assert outputs[1]["result"]["structuredContent"] == {"result": 5}


async def test_notification_gets_no_reply(registry):
    outputs: list[Mapping[str, Any]] = []
    reader = feed(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2025-11-25"},
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
    )
    await serve_stdio(registry, reader, outputs.append)

    assert len(outputs) == 1  # only the initialize reply -- the notification gets none


class CountingAdapterSet(AdapterSet):
    """Record adapter selections to verify legacy connection pinning."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.seen: list[Any] = []

    def select(self, pre: Any) -> Any:
        self.seen.append(pre)
        return super().select(pre)


async def test_adapter_is_selected_once_per_session(registry):
    """Legacy stdio keeps the initially selected adapter."""
    adapters = CountingAdapterSet.default()

    outputs: list[Mapping[str, Any]] = []
    reader = feed(
        {"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "ping", "params": {}},
        {"jsonrpc": "2.0", "id": 3, "method": "ping", "params": {}},
    )
    await serve_stdio(registry, reader, outputs.append, adapters=adapters)

    assert len(adapters.seen) == 1
    assert len(outputs) == 3


async def test_malformed_first_message_falls_back_cleanly(registry):
    """Report PARSE using the fallback codec, without HTTP header checks."""
    outputs: list[Mapping[str, Any]] = []
    reader = asyncio.StreamReader()
    reader.feed_data(b"not json at all\n")
    reader.feed_eof()

    await serve_stdio(registry, reader, outputs.append)

    assert len(outputs) == 1
    assert outputs[0]["error"]["code"] == -32700


async def test_call_tool_bad_arguments_is_a_result_not_an_error(registry):
    outputs: list[Mapping[str, Any]] = []
    reader = feed(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2025-11-25"},
        },
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "add", "arguments": {"a": "not-a-number"}},
        },
    )
    await serve_stdio(registry, reader, outputs.append)

    assert "error" not in outputs[1]
    assert outputs[1]["result"]["isError"] is True


async def test_empty_input_produces_no_output(registry):
    outputs: list[Mapping[str, Any]] = []
    reader = asyncio.StreamReader()
    reader.feed_eof()
    await serve_stdio(registry, reader, outputs.append)
    assert outputs == []


@pytest.mark.parametrize(
    ("version", "params_meta"),
    [
        ("2025-11-25", None),
        (
            "2026-07-28",
            {
                "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                "io.modelcontextprotocol/clientCapabilities": {},
            },
        ),
    ],
)
async def test_streaming_tool_reports_progress_on_every_revision(registry, version, params_meta):
    """Both sequential and concurrent execution paths must attach a notification channel."""
    outputs: list[Mapping[str, Any]] = []
    call_params: dict[str, Any] = {"name": "counter", "arguments": {"a": 0, "b": 3}}
    if params_meta is not None:
        call_params["_meta"] = params_meta
    messages: list[dict[str, Any]] = []
    if params_meta is None:
        messages.append(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": version},
            }
        )
    messages.append({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": call_params})

    await serve_stdio(registry, feed(*messages), outputs.append, adapters=AdapterSet.default())

    progress = [m for m in outputs if m.get("method") == "notifications/progress"]
    assert len(progress) == 3
    assert [m["params"]["progress"] for m in progress] == [0, 1, 2]
    assert outputs[-1]["id"] == 2


async def test_a_listing_pages_over_stdio_too(registry):
    """Paging is the dispatcher's, so a transport without HTTP has it too."""
    registry.page_size = 1
    outputs: list[Mapping[str, Any]] = []
    reader = feed(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2025-11-25"},
        },
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
    )
    await serve_stdio(registry, reader, outputs.append)

    first = outputs[1]["result"]
    assert len(first["tools"]) == 1
    cursor = first["nextCursor"]
    assert cursor

    outputs.clear()
    reader = feed(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2025-11-25"},
        },
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {"cursor": cursor}},
    )
    await serve_stdio(registry, reader, outputs.append)

    second = outputs[1]["result"]
    assert len(second["tools"]) == 1
    assert second["tools"][0]["name"] > first["tools"][0]["name"], "the page after, in order"
