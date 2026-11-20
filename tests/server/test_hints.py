"""Tool annotation composition, defaults, and wire names."""

from __future__ import annotations

import json

import pytest
from pydantic import BaseModel

from aiohttp_tiny_mcp import (
    Hint,
    MemoryHub,
    MemorySessionStore,
    Registry,
)
from aiohttp_tiny_mcp.protocol.selection import AdapterSet
from aiohttp_tiny_mcp.testing import connect

pytestmark = pytest.mark.asyncio

MODERN = AdapterSet.default().by_version["2026-07-28"]


class Nothing(BaseModel):
    """No arguments."""


async def test_one_hint_needs_no_unwrapping():
    assert dict(Hint.READ_ONLY) == {"readOnlyHint": True}


async def test_hints_glue_together():
    assert dict(Hint.READ_ONLY | Hint.IDEMPOTENT) == {
        "readOnlyHint": True,
        "idempotentHint": True,
    }


async def test_gluing_keeps_going():
    joined = Hint.READ_ONLY | Hint.IDEMPOTENT | ~Hint.OPEN_WORLD
    assert dict(joined) == {
        "readOnlyHint": True,
        "idempotentHint": True,
        "openWorldHint": False,
    }


async def test_a_hint_is_denied_with_a_tilde():
    assert dict(~Hint.DESTRUCTIVE) == {"destructiveHint": False}


async def test_a_plain_mapping_still_works():
    joined = Hint.READ_ONLY | {"futureHint": True}
    assert dict(joined) == {"readOnlyHint": True, "futureHint": True}
    assert dict({"futureHint": True} | Hint.READ_ONLY) == {
        "futureHint": True,
        "readOnlyHint": True,
    }


async def test_what_is_glued_cannot_be_changed_afterwards():
    """Shared annotations must not let one tool mutate another."""
    joined = Hint.READ_ONLY | Hint.IDEMPOTENT
    with pytest.raises(TypeError):
        joined["readOnlyHint"] = False  # type: ignore[index]


async def test_the_key_reaching_a_client_is_the_specification_s():
    assert json.dumps(dict(Hint.READ_ONLY)) == '{"readOnlyHint": true}'


async def test_a_client_reads_what_the_tool_declared():
    registry = Registry("t", "1", hub=MemoryHub(), session_store=MemorySessionStore())

    async def look(args: Nothing) -> str:
        """Read something, and change nothing at all."""
        return "looked"

    async def wipe(args: Nothing) -> str:
        """Remove everything. This cannot be undone."""
        return "wiped"

    registry.tool(look, annotations=Hint.READ_ONLY | Hint.IDEMPOTENT)
    registry.tool(wipe, annotations=Hint.DESTRUCTIVE | ~Hint.OPEN_WORLD)

    async with connect(registry, adapter=MODERN) as client:
        tools = {tool.name: tool for tool in await client.list_tools()}

    assert tools["look"].annotations == {"readOnlyHint": True, "idempotentHint": True}
    assert tools["wipe"].annotations == {"destructiveHint": True, "openWorldHint": False}
