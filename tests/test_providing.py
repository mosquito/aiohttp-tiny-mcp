"""Existing-instance dependency registration and type selection."""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from aiohttp_tiny_mcp import (
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


class Daemon:
    def __init__(self, name: str = "real") -> None:
        self.name = name


class Loud(Daemon):
    """Something a handler may still ask for as a `Daemon`."""


def fresh() -> Registry:
    return Registry("t", "1", hub=MemoryHub(), session_store=MemorySessionStore())


async def serve(registry: Registry, name: str, arguments: dict | None = None):
    """One call against a registry, with nothing in between to set up."""
    async with connect(registry, adapter=MODERN) as client:
        return await client.call_tool(name, arguments or {})


async def test_the_type_comes_from_the_object():
    registry = fresh()
    daemon = Daemon()
    registry.provide_instance(daemon)

    async def which(args: Nothing, docker: Daemon) -> str:
        """Report which daemon was supplied."""
        return docker.name

    registry.tool(which)
    assert (await serve(registry, "which")).content[0].text == "real"


async def test_the_same_object_reaches_every_call():
    registry = fresh()
    seen: list[int] = []
    daemon = Daemon()
    registry.provide_instance(daemon)

    async def note(args: Nothing, docker: Daemon) -> str:
        """Note which object arrived."""
        seen.append(id(docker))
        return "noted"

    registry.tool(note)
    await serve(registry, "note")
    await serve(registry, "note")
    assert seen == [id(daemon), id(daemon)]


async def test_an_object_may_be_supplied_as_something_wider():
    registry = fresh()
    registry.provide_instance(Loud("loud"), Daemon)

    async def which(args: Nothing, docker: Daemon) -> str:
        """Report which daemon was supplied."""
        return docker.name

    registry.tool(which)
    assert (await serve(registry, "which")).content[0].text == "loud"


async def test_a_class_passed_by_mistake_is_refused():
    """A class would infer type rather than the dependency callers need."""
    with pytest.raises(TypeError, match="rather than an instance"):
        fresh().provide_instance(Daemon)


async def test_the_class_itself_can_still_be_provided_on_purpose():
    registry = fresh()
    registry.provide_instance(Daemon, type)

    async def named(args: Nothing, kind: type) -> str:
        """Report the class that was supplied."""
        return kind.__name__

    registry.tool(named)
    assert (await serve(registry, "named")).content[0].text == "Daemon"


async def test_a_missing_provider_names_both_ways_of_supplying_one():
    registry = fresh()

    async def which(args: Nothing, docker: Daemon) -> str:
        """Report which daemon was supplied."""
        return docker.name

    with pytest.raises(TypeError, match="provide_instance"):
        registry.tool(which)
