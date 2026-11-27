"""What 2025-06-18 and the two revisions before it have no field for.

Icons arrived in 2025-11-25, on a tool, a resource, a resource template, a prompt and the
server info, together with `websiteUrl` on the server info. A display title of the server
arrived one revision earlier, in 2025-06-18.

Sending a field a revision never defined is not harmless. A strict client rejects the
message; a lenient one shows a person an image it was never told it could show.
"""

from __future__ import annotations

import pytest

from aiohttp_tiny_mcp import Client
from aiohttp_tiny_mcp.protocol.core import Call, ClientInfo, Operation
from aiohttp_tiny_mcp.protocol.models import InitializeParams
from aiohttp_tiny_mcp.protocol.selection import AdapterSet

pytestmark = pytest.mark.asyncio

WITH_ICONS = ("2026-07-28", "2025-11-25")

WITHOUT_ICONS = ("2025-06-18", "2025-03-26", "2024-11-05")

ADAPTERS = AdapterSet.default().adapters


def carries_icons(version: str) -> bool:
    """Every revision is in one list or the other, so a new one fails here first."""
    assert version in WITH_ICONS + WITHOUT_ICONS, f"decide where {version} stands"
    return version in WITH_ICONS


@pytest.mark.parametrize("adapter", ADAPTERS, ids=lambda a: a.version)
async def test_a_declaration_carries_an_icon_only_where_the_revision_has_the_field(
    adapter, projection_registry, real_endpoint
):
    url = await real_endpoint(projection_registry)
    expected = carries_icons(adapter.version)
    async with Client(url, adapter) as client:
        await client.initialize()
        tools = {tool.name: tool for tool in await client.list_tools()}
        resources = await client.list_resources()
        templates = await client.list_resource_templates()
        prompts = await client.list_prompts()

    assert (tools["described"].icons is not None) is expected
    assert (resources[0].icons is not None) is expected
    assert (templates[0].icons is not None) is expected
    assert (prompts[0].icons is not None) is expected


async def test_an_icon_keeps_what_was_declared(projection_registry, real_endpoint):
    url = await real_endpoint(projection_registry)
    async with Client(url, AdapterSet.default().by_version["2025-11-25"]) as client:
        await client.initialize()
        tools = {tool.name: tool for tool in await client.list_tools()}

    icon = tools["described"].icons[0]
    assert icon.src == "https://example.test/i.png"
    assert icon.sizes == ["48x48"]
    assert icon.mime_type == "image/png"


@pytest.mark.parametrize("version", ["2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"])
async def test_the_server_info_carries_icons_only_where_the_revision_has_the_field(
    version, projection_registry
):
    """Read the handshake result directly: the field is on the server info, not the tool."""
    adapter = AdapterSet.default().by_version[version]
    call = Call(
        operation=Operation.DESCRIBE,
        id=1,
        target=None,
        arguments={},
        params=InitializeParams(protocol_version=version),
        client=ClientInfo(),
    )
    info = adapter.describe_server(projection_registry, call).server_info

    assert (info.icons is not None) is carries_icons(version)
    assert (info.website_url is not None) is carries_icons(version)


@pytest.mark.parametrize("version", ["2025-06-18", "2025-03-26"])
async def test_a_server_title_stops_at_2025_03_26(version, projection_registry):
    """The server title arrived in 2025-06-18, before icons."""
    projection_registry.info = projection_registry.info.model_copy(update={"title": "Projection"})
    adapter = AdapterSet.default().by_version[version]
    call = Call(
        operation=Operation.DESCRIBE,
        id=1,
        target=None,
        arguments={},
        params=InitializeParams(protocol_version=version),
        client=ClientInfo(),
    )
    info = adapter.describe_server(projection_registry, call).server_info

    assert (info.title is not None) is (version == "2025-06-18")


async def test_the_modern_revision_carries_the_server_icons_in_meta(
    projection_registry, real_endpoint
):
    """2026-07-28 has no `serverInfo` result field: the info rides in `_meta`."""
    url = await real_endpoint(projection_registry)
    async with Client(url, AdapterSet.default().by_version["2026-07-28"]) as client:
        discovery = await client.initialize()

    info = discovery["_meta"]["io.modelcontextprotocol/serverInfo"]
    assert info["icons"][0]["src"] == "https://example.test/i.png"
    assert info["websiteUrl"] == "https://example.test"
