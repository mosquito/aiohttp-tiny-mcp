"""Exercise serve_stdio with in-memory reader/writer fixtures."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import warnings
from collections.abc import Mapping
from typing import Any

import pytest
from pydantic import BaseModel

from aiohttp_tiny_mcp import ClientError, Exchange, Registry, StdioClient
from aiohttp_tiny_mcp.protocol.selection import AdapterSet
from aiohttp_tiny_mcp.server.stdio import serve_stdio, stdin_reader
from aiohttp_tiny_mcp.testing import connect

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(10)]


def feed(*messages: dict[str, Any]) -> asyncio.StreamReader:
    reader = asyncio.StreamReader()
    for msg in messages:
        reader.feed_data((json.dumps(msg) + "\n").encode())
    reader.feed_eof()
    return reader


class Nothing(BaseModel):
    pass


@pytest.mark.parametrize(
    ("resources", "prompts", "enabled"),
    [
        (None, None, True),
        ("tool", None, True),
        ("native", "resource", True),
        ("tool", "resource", True),
        ("tool", "resource", False),
    ],
)
async def test_run_stdio_selects_resource_and_prompt_modes(tmp_path, resources, prompts, enabled):
    script = tmp_path / "stdio_modes.py"
    script.write_text(
        f"""import asyncio
from pydantic import BaseModel
from aiohttp_tiny_mcp import Registry, run_stdio

class Empty(BaseModel):
    pass

registry = Registry("test", "1", resource_tools={enabled!r})

@registry.resource("file://example")
async def example(args: Empty) -> str:
    return "ordinary resource"

@registry.prompt
async def guide(args: Empty) -> str:
    return "prompt message"

asyncio.run(run_stdio(registry, resources={resources!r}, prompts={prompts!r}))
"""
    )
    adapter = AdapterSet.default().by_version["2026-07-28"]
    async with StdioClient.spawn(sys.executable, str(script), adapter=adapter) as client:
        capabilities = (await client.initialize())["capabilities"]
        uri = "mcp-prompt:///guide"
        prompt = await client.get_prompt("guide", {})
        assert prompt.messages[0].content.text == "prompt message"
        if resources == "tool" and enabled:
            assert "resources" not in capabilities
            assert {tool.name for tool in await client.list_tools()} == {
                "mcp_resources_list",
                "mcp_resources_read",
            }
            data = await client.call_tool("mcp_resources_list", {})
            assert {
                item.get("uriTemplate", item.get("uri"))
                for item in data.structured_content["items"]
            } == {
                "file://example",
                uri,
            }
            result = await client.call_tool("mcp_resources_read", {"uri": uri})
            assert result.structured_content["messages"][0]["content"]["text"] == "prompt message"
            with pytest.raises(ClientError):
                await client.list_resources()
        else:
            assert "resources" in capabilities
            assert await client.list_tools() == []
            assert [item.uri for item in await client.list_resources()] == ["file://example"]
            templates = await client.list_resource_templates()
            if prompts == "resource" and enabled:
                assert [item.uri_template for item in templates] == [uri]
                result = await client.read_resource(uri)
                assert json.loads(result.contents[0].text)["messages"][0]["content"]["text"] == (
                    "prompt message"
                )
            else:
                assert templates == []


@pytest.mark.parametrize("option", ["resources", "prompts"])
async def test_serve_stdio_rejects_an_unknown_mode(option):
    with pytest.raises(ValueError, match=option):
        await serve_stdio(Registry("test", "1"), feed(), lambda reply: None, **{option: "unknown"})


@pytest.mark.parametrize("version", AdapterSet.default().versions)
async def test_a_handler_uses_the_revision_the_client_requested(version):
    registry = Registry("revision", "1.0")

    @registry.tool
    async def revision(args: Nothing, ex: Exchange) -> str:
        """Report the adapter that serves this call."""
        return ex.adapter.version

    async with connect(registry, adapter=version) as client:
        answered = await client.call_tool("revision", {})
    assert answered.content[0].text == version


async def test_a_malformed_revision_gets_a_protocol_error():
    outputs = []
    reader = feed(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": ["2025-11-25"]},
        }
    )
    await serve_stdio(Registry("revision", "1.0"), reader, outputs.append)
    assert outputs[0]["error"]["code"] == -32602


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


async def test_stdin_reader_reads_the_process_stdin(monkeypatch):
    """Read one line through `stdin_reader`, from a thread of its own.

    The thread runs `asyncio.run`, so it has no thread-default loop. That is
    the condition under which a loop lookup can pick the wrong loop, and the
    run must stay free of deprecation warnings.
    """
    read_fd, write_fd = os.pipe()
    os.write(write_fd, b"hello\n")
    os.close(write_fd)
    seen: list[bytes] = []

    def in_its_own_thread() -> None:
        async def attach() -> bytes:
            with warnings.catch_warnings():
                warnings.simplefilter("error", DeprecationWarning)
                reader = await stdin_reader()
            return await reader.readline()

        seen.append(asyncio.run(attach()))

    with open(read_fd, "rb", buffering=0) as pipe:
        monkeypatch.setattr(sys, "stdin", pipe)
        await asyncio.to_thread(in_its_own_thread)

    assert seen == [b"hello\n"]
