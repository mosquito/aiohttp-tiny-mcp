from __future__ import annotations

import importlib
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from aiohttp_tiny_mcp import Client, Endpoint, elicit_accept, elicit_decline
from aiohttp_tiny_mcp.protocol.models import CallToolResult, TextContent
from aiohttp_tiny_mcp.protocol.selection import AdapterSet


@pytest.mark.asyncio
@pytest.mark.parametrize("confirmed", [True, False], ids=["accept", "decline"])
async def test_demo_deploy_can_resume_on_another_endpoint_with_no_state_at_all(
    monkeypatch, confirmed
):
    monkeypatch.syspath_prepend(str(Path(__file__).parents[1] / "examples"))
    demo = importlib.import_module("demo")
    adapter = AdapterSet.default().by_version["2026-07-28"]

    async with (
        TestServer(Endpoint(demo.registry).app()) as first_server,
        TestServer(Endpoint(demo.registry).app()) as second_server,
    ):
        async with Client(str(first_server.make_url("/mcp")), adapter) as client:
            first = await client.call_tool("deploy", {"service": "foo"})
        assert isinstance(first, dict)
        assert first["resultType"] == "input_required"
        assert "requestState" not in first

        answer = elicit_accept({"ok": True}) if confirmed else elicit_decline()
        async with Client(str(second_server.make_url("/mcp")), adapter) as client:
            result = await client.call_tool(
                "deploy", {"service": "foo"}, input_responses={"confirm": answer}
            )
        assert isinstance(result, CallToolResult)
        assert not result.is_error
        content = result.content[0]
        assert isinstance(content, TextContent)
        if confirmed:
            assert content.text == "deployed foo"
        else:
            assert content.text == "did not deploy foo: client sent decline"


@pytest.mark.asyncio
@pytest.mark.parametrize("confirmed", [True, False], ids=["accept", "decline"])
async def test_notebook_confirmation_resumes_with_no_state_at_all(monkeypatch, tmp_path, confirmed):
    monkeypatch.syspath_prepend(str(Path(__file__).parents[1] / "examples"))
    notebook = importlib.import_module("notebook_demo")
    store = notebook.NoteStore(str(tmp_path / "notebook.sqlite3"))
    entry = await store.add("note", "Test", "Disposable fixture", [])
    app = web.Application()
    app[notebook.STORE] = store
    Endpoint(notebook.registry).setup(app)
    adapter = AdapterSet.default().by_version["2026-07-28"]

    async with TestServer(app) as server:
        async with Client(str(server.make_url("/mcp")), adapter) as client:
            first = await client.call_tool("delete_entry", {"id": entry.id})
            assert isinstance(first, dict)
            assert first["resultType"] == "input_required"
            assert "requestState" not in first
            answer = elicit_accept({"ok": True}) if confirmed else elicit_decline()
            result = await client.call_tool(
                "delete_entry", {"id": entry.id}, input_responses={"confirm": answer}
            )
            assert isinstance(result, CallToolResult)
            assert not result.is_error
    assert (await store.get(entry.id) is None) is confirmed
