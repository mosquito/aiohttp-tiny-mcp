"""Cursor paging, against the specification's pagination page.

Off unless `Registry(page_size=...)` turns it on. The cursor is opaque, a
missing `nextCursor` is the end, and an unknown cursor is -32602.
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from aiohttp_tiny_mcp import Client, Registry
from aiohttp_tiny_mcp.core import Operation
from aiohttp_tiny_mcp.models import ListParams
from aiohttp_tiny_mcp.protocol.selection import AdapterSet
from aiohttp_tiny_mcp.testing import serving

EVERY = [adapter.version for adapter in AdapterSet.default().adapters]


class Nothing(BaseModel):
    pass


class Ref(BaseModel):
    """A handler's model lives at module level: annotations resolve there."""

    id: str


def with_tools(count: int, page_size: int | None) -> Registry:
    registry = Registry("paged", "1.0", page_size=page_size)
    for index in range(count):

        async def one(args: Nothing) -> str:
            """One of many."""
            return "ok"

        registry.tool(one, name=f"tool{index:02d}")
    return registry


async def test_paging_is_off_unless_asked_for():
    registry = with_tools(4, page_size=None)
    assert registry.page_size is None
    adapter = AdapterSet.default().by_version["2026-07-28"]
    async with serving(registry) as url:
        async with Client(url, adapter) as client:
            await client.initialize()
            result = await client.request(Operation.LIST_TOOLS, ListParams())
    assert len(result["tools"]) == 4
    assert "nextCursor" not in result


async def test_an_entry_added_between_pages_moves_nothing():
    """The cursor names the last entry, not its position, so a registration
    before it neither repeats nor hides anything."""
    registry = with_tools(2, page_size=1)
    adapter = AdapterSet.default().by_version["2026-07-28"]
    async with serving(registry) as url:
        async with Client(url, adapter) as client:
            await client.initialize()
            first = await client.request(Operation.LIST_TOOLS, ListParams())

            async def early(args: Nothing) -> str:
                """Sorts before everything on the first page."""
                return "ok"

            registry.tool(early, name="aaa")
            rest = await client.request(
                Operation.LIST_TOOLS, ListParams(cursor=first["nextCursor"])
            )
    assert [tool["name"] for tool in first["tools"]] == ["tool00"]
    assert [tool["name"] for tool in rest["tools"]] == ["tool01"]


@pytest.mark.parametrize("version", EVERY)
async def test_a_client_reads_every_page(version):
    """Four tools, two at a time, on every revision."""
    registry = with_tools(4, page_size=2)
    adapter = AdapterSet.default().by_version[version]
    seen: list[str] = []
    async with serving(registry) as url:
        async with Client(url, adapter) as client:
            await client.initialize()
            pages = 0
            async for result in client.pages(Operation.LIST_TOOLS):
                pages += 1
                seen += [tool["name"] for tool in result["tools"]]
    assert seen == ["tool00", "tool01", "tool02", "tool03"]
    assert pages == 2, "four entries, two at a time"


@pytest.mark.parametrize("version", EVERY)
async def test_a_cursor_nobody_wrote_is_refused(version):
    """A silent first page would send a paging client round the same entries."""
    from aiohttp_tiny_mcp import ClientError

    registry = with_tools(4, page_size=2)
    adapter = AdapterSet.default().by_version[version]
    async with serving(registry) as url:
        async with Client(url, adapter) as client:
            await client.initialize()
            with pytest.raises(ClientError) as refused:
                await client.request(Operation.LIST_TOOLS, ListParams(cursor="nobody"))
    assert refused.value.code == -32602


async def test_every_listing_pages():
    """Resources, templates and prompts carry it as tools do."""
    registry = Registry("paged", "1.0", page_size=1)

    async def fixed(args: Nothing) -> str:
        """A resource at a known address."""
        return "x"

    async def templated(args: Ref) -> str:
        """A family of addresses."""
        return "x"

    async def greeting(args: Nothing) -> str:
        """A prompt."""
        return "hello"

    for index in range(2):
        registry.resource(f"res://fixed/{index}", fixed, name=f"fixed{index}")
        registry.resource(f"res://{index}/{{id}}", templated, name=f"template{index}")
        registry.prompt(greeting, name=f"prompt{index}")

    adapter = AdapterSet.default().by_version["2026-07-28"]
    async with serving(registry) as url:
        async with Client(url, adapter) as client:
            await client.initialize()
            for operation, field in (
                (Operation.LIST_RESOURCES, "resources"),
                (Operation.LIST_RESOURCE_TEMPLATES, "resourceTemplates"),
                (Operation.LIST_PROMPTS, "prompts"),
            ):
                result = await client.request(operation, ListParams())
                assert len(result[field]) == 1, operation
                assert result.get("nextCursor") is not None, operation
