"""Complete tool catalogues larger than asyncio's default 64 KiB line limit."""

from __future__ import annotations

import json
import sys
from textwrap import dedent

import pytest
from pydantic import BaseModel

from aiohttp_tiny_mcp import Client, Registry, StdioClient
from aiohttp_tiny_mcp.protocol.core import Operation
from aiohttp_tiny_mcp.protocol.models import ListParams
from aiohttp_tiny_mcp.protocol.selection import AdapterSet
from aiohttp_tiny_mcp.testing import serving

ADAPTERS = AdapterSet.default().adapters


class Nothing(BaseModel):
    pass


def catalogue() -> Registry:
    registry = Registry("catalogue", "1")

    async def tool(args: Nothing) -> str:
        return "ok"

    for index in range(26):
        registry.tool(tool, name=f"tool{index:02}", description="x" * 3000)
    return registry


async def check_catalogue(client):
    await client.initialize()
    result = await client.request(Operation.LIST_TOOLS, ListParams())
    assert len(json.dumps(result).encode()) > 65536
    assert "nextCursor" not in result
    assert [tool["name"] for tool in result["tools"]] == [f"tool{i:02}" for i in range(26)]
    assert len(await client.list_tools()) == 26


@pytest.mark.parametrize("adapter", ADAPTERS, ids=lambda a: a.version)
async def test_complete_catalogue_over_http(adapter):
    async with serving(catalogue()) as url:
        async with Client(url, adapter) as client:
            await check_catalogue(client)


@pytest.fixture
def catalogue_script(tmp_path):
    script = tmp_path / "catalogue.py"
    script.write_text(
        dedent("""\
        import asyncio
        from pydantic import BaseModel
        from aiohttp_tiny_mcp import Registry, run_stdio
        class Nothing(BaseModel):
            pass
        registry = Registry("catalogue", "1")
        async def tool(args: Nothing) -> str:
            return "ok"
        for index in range(26):
            registry.tool(tool, name=f"tool{index:02}", description="x" * 3000)
        asyncio.run(run_stdio(registry))
        """)
    )
    return str(script)


@pytest.mark.parametrize("adapter", ADAPTERS, ids=lambda a: a.version)
async def test_complete_catalogue_over_stdio(adapter, catalogue_script):
    async with StdioClient.spawn(sys.executable, catalogue_script, adapter=adapter) as client:
        await check_catalogue(client)


async def test_stdio_reader_limit_is_configurable(catalogue_script):
    adapter = ADAPTERS[0]
    async with StdioClient.spawn(
        sys.executable, catalogue_script, adapter=adapter, limit=65536
    ) as client:
        await client.initialize()
        with pytest.raises(ValueError, match="(separator|Separator)"):
            await client.list_tools()

    async with StdioClient.spawn(
        sys.executable, catalogue_script, adapter=adapter, limit=128 * 1024
    ) as client:
        await check_catalogue(client)
