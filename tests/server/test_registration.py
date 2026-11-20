"""Register handlers directly, including bound methods."""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from aiohttp_tiny_mcp import Client, MemoryHub, MemorySessionStore, Registry
from aiohttp_tiny_mcp.protocol.selection import AdapterSet

pytestmark = pytest.mark.asyncio

MODERN = AdapterSet.default().by_version["2026-07-28"]


class Add(BaseModel):
    a: int
    b: int


class Nothing(BaseModel):
    pass


class Ref(BaseModel):
    id: int


class Lang(BaseModel):
    language: str


async def add(args: Add) -> int:
    """Add two integers."""
    return args.a + args.b


async def config(args: Nothing) -> dict:
    """Application configuration."""
    return {"debug": False}


async def item(args: Ref) -> str:
    """One item by id."""
    return f"item-{args.id}"


async def greet(args: Lang) -> str:
    """Ask for a greeting."""
    return f"Hello, {args.language} speaker."


def fresh() -> Registry:
    return Registry("plain", "1.0", hub=MemoryHub(), session_store=MemorySessionStore())


async def test_everything_registers_by_argument(real_endpoint):
    registry = fresh()
    registry.tool(add)
    registry.resource("config://app", config, mime_type="application/json")
    registry.resource("catalog://items/{id}", item, name="item")
    registry.prompt(greet)

    async with Client(await real_endpoint(registry), MODERN) as client:
        await client.initialize()
        assert {tool.name for tool in await client.list_tools()} == {"add"}
        assert {r.uri for r in await client.list_resources()} == {"config://app"}
        assert {p.name for p in await client.list_prompts()} == {"greet"}
        assert (await client.call_tool("add", {"a": 2, "b": 3})).content[0].text == "5"
        read = await client.read_resource("catalog://items/7")
        assert "item-7" in read.contents[0].text


async def test_a_name_may_be_given_where_the_function_name_will_not_do():
    registry = fresh()
    registry.tool(add, name="sum", title="Add up")
    assert set(registry.tools) == {"sum"}
    assert registry.tools["sum"].title == "Add up"


async def test_the_decorator_form_still_works():
    registry = fresh()

    @registry.tool
    async def one(args: Nothing) -> str:
        """One."""
        return "1"

    @registry.resource("config://app")
    async def two(args: Nothing) -> dict:
        """Two."""
        return {}

    @registry.prompt
    async def three(args: Lang) -> str:
        """Three."""
        return "3"

    assert set(registry.tools) == {"one"}
    assert set(registry.resources_fixed) == {"config://app"}
    assert set(registry.prompts) == {"three"}


async def test_a_handler_may_be_a_method_that_holds_its_own_state(real_endpoint):

    class Counter:
        def __init__(self, start: int) -> None:
            self.total = start

        async def bump(self, args: Nothing) -> int:
            """Increase and report."""
            self.total += 1
            return self.total

    counter = Counter(41)
    registry = fresh()
    registry.tool(counter.bump, name="bump")

    async with Client(await real_endpoint(registry), MODERN) as client:
        await client.initialize()
        assert (await client.call_tool("bump", {})).content[0].text == "42"
    assert counter.total == 42


async def test_the_same_handler_may_be_registered_twice_under_two_names():
    registry = fresh()
    registry.tool(add, name="add")
    registry.tool(add, name="plus")
    assert set(registry.tools) == {"add", "plus"}


async def test_a_duplicate_is_still_refused():
    registry = fresh()
    registry.tool(add)
    with pytest.raises(ValueError, match="duplicate tool"):
        registry.tool(add)

    registry.resource("config://app", config)
    with pytest.raises(ValueError, match="duplicate resource"):
        registry.resource("config://app", config)
