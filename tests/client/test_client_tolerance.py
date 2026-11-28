"""What the client accepts from a server it does not control."""

from __future__ import annotations

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from pydantic import BaseModel

from aiohttp_tiny_mcp import (
    BlobResourceContents,
    CallToolResult,
    Client,
    ClientError,
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


@pytest.mark.parametrize("adapter", ADAPTERS, ids=lambda a: a.version)
async def test_an_unsupported_request_does_not_reach_the_question_callback(adapter):
    replies = []

    async def on_ask(request):
        raise AssertionError("an unsupported method is not an elicitation request")

    async def reply(envelope):
        replies.append(envelope)

    client = Client("http://localhost/mcp", adapter, on_ask=on_ask)
    client.reply = reply
    await client.incoming({"jsonrpc": "2.0", "id": "unsupported", "method": "roots/list"})
    assert replies == [
        {
            "jsonrpc": "2.0",
            "id": "unsupported",
            "error": {"code": -32601, "message": "unsupported: roots/list"},
        }
    ]


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


@pytest.mark.parametrize(
    ("status", "body", "content_type", "named"),
    [
        (502, "<html>bad gateway</html>", "text/html", "502 Bad Gateway"),
        (404, "Not Found", "text/plain", "404 Not Found"),
        (200, "", "text/plain", "200 OK"),
        (500, "a" * 4000, "text/plain", "500 Internal Server Error"),
    ],
)
async def test_a_reply_that_is_not_json_names_its_status(status, body, content_type, named) -> None:
    """A mistyped address, a proxy page and a gateway error all looked like a parse error.

    The message names the status and the address, so the reader knows whether to fix the
    URL or the server.
    """

    async def answer(request: web.Request) -> web.Response:
        return web.Response(status=status, text=body, content_type=content_type)

    app = web.Application()
    app.router.add_route("*", "/mcp", answer)
    async with TestServer(app) as server:
        async with Client(str(server.make_url("/mcp")), ADAPTERS[0]) as client:
            with pytest.raises(ClientError, match=named) as reported:
                await client.initialize()
    assert reported.value.data == {"status": status}
    assert len(str(reported.value)) < 400, "the excerpt is bounded"


async def test_the_address_in_the_message_carries_no_credential() -> None:
    """A query may hold a token, and an exception message travels into logs."""

    async def answer(request: web.Request) -> web.Response:
        return web.Response(status=502, text="nope", content_type="text/plain")

    app = web.Application()
    app.router.add_route("*", "/mcp", answer)
    async with TestServer(app) as server:
        url = str(server.make_url("/mcp")) + "?access_token=secret-value"
        async with Client(url, ADAPTERS[0]) as client:
            with pytest.raises(ClientError) as reported:
                await client.initialize()
    assert "secret-value" not in str(reported.value)


async def test_a_json_rpc_error_keeps_its_own_code() -> None:
    """A protocol error carries a code already, whatever HTTP status it arrives with."""

    async def answer(request: web.Request) -> web.Response:
        return web.json_response(
            {"jsonrpc": "2.0", "id": 1, "error": {"code": -32021, "message": "declare something"}},
            status=400,
        )

    app = web.Application()
    app.router.add_route("*", "/mcp", answer)
    async with TestServer(app) as server:
        async with Client(str(server.make_url("/mcp")), ADAPTERS[0]) as client:
            with pytest.raises(ClientError, match="declare something") as reported:
                await client.initialize()
    assert reported.value.code == -32021
