"""Console asset serving and browser-origin validation."""

from __future__ import annotations

import pytest
from aiohttp.test_utils import TestClient, TestServer

from aiohttp_tiny_mcp import Endpoint
from aiohttp_tiny_mcp.console import Console

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def served(registry):
    app = Endpoint(registry).app("/mcp")
    Console("/mcp").setup(app, "/console")
    async with TestClient(TestServer(app)) as client:
        yield client


async def test_the_page_and_its_two_files_are_served(served):
    for path, kind in (
        ("/console", "text/html"),
        ("/console/console.css", "text/css"),
        ("/console/console.js", "application/javascript"),
    ):
        response = await served.get(path)
        assert response.status == 200, path
        assert response.content_type == kind
        assert await response.text()


async def test_the_page_is_told_where_the_endpoint_is(served):
    body = await (await served.get("/console")).text()
    assert 'data-endpoint="/mcp"' in body
    assert "{{ENDPOINT}}" not in body


async def test_the_assets_are_addressed_absolutely(served):
    """Without a trailing slash, relative asset URLs resolve against the parent path."""
    body = await (await served.get("/console")).text()
    assert 'href="/console/console.css"' in body
    assert 'src="/console/console.js"' in body
    assert "{{BASE}}" not in body

    with_slash = await (await served.get("/console/")).text()
    assert with_slash == body


async def test_the_console_is_mounted_wherever_it_is_asked_for(registry):
    app = Endpoint(registry).app("/rpc")
    Console("/rpc").setup(app, "/admin/try")
    async with TestClient(TestServer(app)) as client:
        body = await (await client.get("/admin/try")).text()
        assert 'href="/admin/try/console.css"' in body
        assert 'data-endpoint="/rpc"' in body
        assert (await client.get("/admin/try/console.css")).status == 200


async def test_anything_else_under_the_console_is_not_served(served):
    assert (await served.get("/console/../secrets")).status == 404
    assert (await served.get("/console/console.py")).status == 404


async def request_with_origin(client, origin: str | None):
    headers = {"MCP-Protocol-Version": "2025-11-25", "Accept": "application/json"}
    if origin is not None:
        headers["Origin"] = origin
    return await client.post(
        "/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {}}, headers=headers
    )


async def test_a_page_this_server_served_is_allowed(served):
    response = await request_with_origin(served, str(served.make_url("")).rstrip("/"))
    assert response.status == 200


async def test_a_page_from_anywhere_else_is_refused(served):
    """DNS rebinding retains the attacker's Origin header."""
    response = await request_with_origin(served, "http://attacker.example")
    assert response.status == 403


async def test_no_origin_at_all_is_unaffected(served):
    assert (await request_with_origin(served, None)).status == 200


async def test_another_origin_may_still_be_allowed_explicitly(registry):
    app = Endpoint(registry, allowed_origins={"https://app.example"}).app("/mcp")
    async with TestClient(TestServer(app)) as client:
        assert (await request_with_origin(client, "https://app.example")).status == 200
        assert (await request_with_origin(client, "https://other.example")).status == 403


async def test_the_page_carries_the_title_and_description_it_was_given(registry):
    app = Endpoint(registry).app("/mcp")
    Console(
        "/mcp",
        title="Docker",
        description="This host's containers.",
    ).setup(app, "/console")
    async with TestClient(TestServer(app)) as client:
        body = await (await client.get("/console")).text()
    assert "<title>Docker</title>" in body
    assert 'title="Show server instructions" disabled>Docker</button></h1>' in body
    assert "This host&#x27;s containers." in body


async def test_what_a_deployment_says_is_escaped(registry):
    app = Endpoint(registry).app("/mcp")
    Console("/mcp", title="<script>alert(1)</script>").setup(app, "/console")
    async with TestClient(TestServer(app)) as client:
        body = await (await client.get("/console")).text()
    assert "<script>alert(1)</script>" not in body
    assert "&lt;script&gt;" in body


async def test_a_deployment_may_serve_its_own_page(registry, tmp_path):
    """Custom HTML still uses bundled CSS and JavaScript."""
    template = tmp_path / "mine.html"
    template.write_text(
        '<html data-endpoint="{{ENDPOINT}}">'
        '<link href="{{BASE}}/console.css"><h1>{{TITLE}}</h1>'
        '<p>{{SUPPORT}}</p><script src="{{BASE}}/console.js"></script></html>',
        encoding="utf-8",
    )
    app = Endpoint(registry).app("/mcp")
    Console(
        "/mcp",
        title="Ours",
        template=template,
        variables={"SUPPORT": "ops@example.com"},
    ).setup(app, "/console")
    async with TestClient(TestServer(app)) as client:
        body = await (await client.get("/console")).text()
        assert (await client.get("/console/console.js")).status == 200
    assert "<h1>Ours</h1>" in body
    assert "ops@example.com" in body
    assert 'data-endpoint="/mcp"' in body
    assert "{{" not in body


async def test_the_console_speaks_every_supported_revision():
    """The browser client and server select from the same revisions."""
    import re
    from pathlib import Path

    from aiohttp_tiny_mcp.protocol.selection import AdapterSet

    source = (
        Path(__file__).parent.parent / "src" / "aiohttp_tiny_mcp" / "console" / "console.js"
    ).read_text(encoding="utf-8")
    served = {version for version in re.findall(r'"(\d{4}-\d{2}-\d{2})":', source)}
    assert served == {adapter.version for adapter in AdapterSet.default().adapters}
