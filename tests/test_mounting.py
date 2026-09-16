"""Where the endpoint ends up when an application mounts it.

`routes()` returns ordinary `RouteDef`s for `add_routes`, and `setup()` is the
shorthand that adds them to an application of its own. A prefix is ordinary
routing for the endpoint itself.

Three addresses around it are ones a client computes or posts to rather than
follows from a link, so each has to be right under `add_subapp` too:

- The console prints its own paths into the HTML.
- The HTTP+SSE stream names the path a client posts messages to.
- The protected-resource metadata path is fixed by RFC 8615 relative to the
  authority, and is the one a prefix must not reach.

The first two are taken from the request, so a subapplication needs no help.
The third cannot be: a client computes it from the resource URL without ever
having seen it, so the root application serves it.

These check what `docs/deployment/transports.md` tells a reader to do.
"""

from __future__ import annotations

import re

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from aiohttp_tiny_mcp import Endpoint, Registry
from aiohttp_tiny_mcp.auth import Authorization, Principal
from aiohttp_tiny_mcp.console import Console
from aiohttp_tiny_mcp.http_sse import SseEndpoint

pytestmark = pytest.mark.asyncio

RESOURCE = "https://mcp.example.com/reports/mcp"


class NoTokens:
    """Verifies nothing. These tests are about routing, not about tokens."""

    async def verify(self, token: str) -> Principal | None:
        return None


@pytest.fixture
def auth() -> Authorization:
    return Authorization(
        verifier=NoTokens(),
        resource=RESOURCE,
        authorization_servers=["https://login.example.com"],
    )


@pytest.fixture
def protected(auth: Authorization) -> Endpoint:
    return Endpoint(Registry("reports", "1.0", auth=auth))


def canonical(app: web.Application) -> list[str]:
    return sorted(resource.canonical for resource in app.router.resources())


async def status(app: web.Application, path: str) -> int:
    async with TestClient(TestServer(app)) as http:
        return (await http.get(path)).status


async def test_add_routes_puts_the_endpoint_where_it_is_asked_for():
    """The explicit form: route definitions, added like any others."""
    app = web.Application()
    app.add_routes(Endpoint(Registry("reports", "1.0")).routes("/reports/mcp"))
    assert canonical(app) == ["/reports/mcp"]


async def test_setup_adds_the_same_routes():
    """`setup` is the shorthand, not a second code path."""
    registry = Registry("reports", "1.0")
    added = web.Application()
    added.add_routes(Endpoint(registry).routes("/reports/mcp"))
    through_setup = Endpoint(registry).setup(web.Application(), "/reports/mcp")
    assert canonical(added) == canonical(through_setup)


async def test_there_are_no_metadata_routes_without_authorization():
    """So an application can add them unconditionally."""
    assert Endpoint(Registry("reports", "1.0")).metadata_routes() == []


async def test_the_metadata_path_follows_the_resource_not_the_mount(auth):
    """A client computes it from the resource URL, so the mount cannot move it."""
    assert auth.metadata_path == "/.well-known/oauth-protected-resource/reports/mcp"
    assert auth.metadata_url == f"https://mcp.example.com{auth.metadata_path}"


async def test_a_prefix_in_the_path_leaves_the_metadata_at_the_root(auth, protected):
    app = web.Application()
    app.add_routes(protected.routes("/reports/mcp"))

    assert canonical(app) == [auth.metadata_path, "/reports/mcp"]
    assert await status(app, auth.metadata_path) == 200


async def test_a_subapplication_would_move_the_metadata_where_no_client_looks(auth, protected):
    """What `metadata=False` is for. A prefix reaches every route a
    subapplication holds, and this is the one that must not take one."""
    host = web.Application()
    host.add_subapp("/reports/", protected.app("/mcp"))

    assert await status(host, auth.metadata_path) == 404
    assert await status(host, f"/reports{auth.metadata_path}") == 200


async def test_the_endpoint_under_a_prefix_with_its_metadata_at_the_root(auth, protected):
    """The explicit split: the endpoint in the subapplication, the metadata on
    the application that owns the root."""
    section = web.Application()
    section.add_routes(protected.routes("/mcp", metadata=False))
    assert canonical(section) == ["/mcp"]

    host = web.Application()
    host.add_subapp("/reports/", section)
    host.add_routes(protected.metadata_routes())

    async with TestClient(TestServer(host)) as http:
        found = await http.get(auth.metadata_path)
        assert found.status == 200
        assert (await found.json())["resource"] == RESOURCE
        assert (await http.post("/reports/mcp", json={})).status == 401


async def test_a_console_addresses_its_own_files_wherever_it_is_mounted():
    """The page carries absolute addresses, taken from the request that asked
    for it, so a prefix it was never told about is still correct."""
    section = web.Application()
    section.add_routes(Endpoint(Registry("reports", "1.0")).routes("/mcp"))
    section.add_routes(Console("/reports/mcp").routes("/console"))

    host = web.Application()
    host.add_subapp("/reports/", section)

    async with TestClient(TestServer(host)) as http:
        page = await (await http.get("/reports/console")).text()
        assets = sorted(set(re.findall(r'(?:src|href)="(/[^"]*)"', page)))
        assert assets == ["/reports/console/console.css", "/reports/console/console.js"]
        for address in assets:
            assert (await http.get(address)).status == 200, address


async def test_the_sse_stream_names_a_posting_path_a_client_can_reach():
    """The address on the stream is one the client posts to, so it carries the
    prefix the stream itself was reached through."""
    section = web.Application()
    section.add_routes(SseEndpoint(Registry("reports", "1.0")).routes())

    host = web.Application()
    host.add_subapp("/legacy/", section)

    async with TestClient(TestServer(host)) as http:
        stream = await http.get("/legacy/sse", headers={"Accept": "text/event-stream"})
        try:
            assert (await stream.content.readline()).strip() == b"event: endpoint"
            told = (await stream.content.readline()).decode().strip()
        finally:
            stream.close()
    assert told.startswith("data: /legacy/messages?session_id=")
