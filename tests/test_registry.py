from __future__ import annotations

import pytest
from pydantic import BaseModel

from aiohttp_tiny_mcp import Exchange, MemorySessionStore, Registry
from aiohttp_tiny_mcp.protocol.models import CompleteParams
from aiohttp_tiny_mcp.server.registry import Registry as ModuleRegistry
from aiohttp_tiny_mcp.storage.hub import MemoryHub


class NoArguments(BaseModel):
    """Annotations are resolved in this module, so a handler's argument model
    has to live here rather than inside a test."""


class Args(BaseModel):
    pass


class ItemArgs(BaseModel):
    id: str


async def handler(args: Args) -> str:
    return "ok"


async def item_handler(args: ItemArgs) -> str:
    return args.id


async def complete(args: CompleteParams) -> list[str]:
    return []


def test_registry_imports_share_exchange_injection():
    assert ModuleRegistry is Registry
    registry = ModuleRegistry("test", "1", hub=MemoryHub(), session_store=MemorySessionStore())

    @registry.tool
    async def contextual(args: Args, ex: Exchange) -> str:
        return ex.registry.info.name

    assert "contextual" in registry.tools


def test_registry_uses_fresh_memory_backends_by_default():
    first = Registry("first", "1")
    second = Registry("second", "1")

    assert isinstance(first.hub, MemoryHub)
    assert isinstance(first.session_store, MemorySessionStore)
    assert first.hub is not second.hub
    assert first.session_store is not second.session_store


def test_duplicate_named_declarations_are_rejected():
    registry = Registry("test", "1", hub=MemoryHub(), session_store=MemorySessionStore())
    registry.tool(handler, name="same")
    with pytest.raises(ValueError, match="duplicate tool"):
        registry.tool(handler, name="same")

    registry.prompt(handler, name="same")
    with pytest.raises(ValueError, match="duplicate prompt"):
        registry.prompt(handler, name="same")


def test_duplicate_resources_and_completion_handlers_are_rejected():
    registry = Registry("test", "1", hub=MemoryHub(), session_store=MemorySessionStore())
    registry.resource("test://fixed")(handler)
    with pytest.raises(ValueError, match="duplicate resource"):
        registry.resource("test://fixed")(handler)

    registry.resource("test://items/{id}")(item_handler)
    with pytest.raises(ValueError, match="duplicate resource template"):
        registry.resource("test://items/{id}")(item_handler)

    registry.completions(complete)
    with pytest.raises(ValueError, match="duplicate completions"):
        registry.completions(complete)


async def test_a_description_loses_the_indentation_of_its_source(registry):

    @registry.tool
    async def explained(args: NoArguments) -> str:
        """One line.

        A second paragraph, written where the code is, and indented
        because the code is.
        """
        return "ok"

    described = registry.tools["explained"].description
    assert described.startswith("One line.\n\nA second paragraph")
    assert "\n    " not in described
    assert described.endswith("because the code is.")
