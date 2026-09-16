"""Self-contained stdio server fixture spawned by test_stdio_client.py."""

from __future__ import annotations

import asyncio

from pydantic import BaseModel

from aiohttp_tiny_mcp import (  # noqa: E402
    Exchange,
    MemoryHub,
    MemorySessionStore,
    NeedInput,
    Registry,
    elicit,
    run_stdio,
)
from aiohttp_tiny_mcp.models import CompleteParams


class Add(BaseModel):
    a: int
    b: int


class Sum(BaseModel):
    result: int


class Nothing(BaseModel):
    pass


class ItemRef(BaseModel):
    id: str


class Confirm(BaseModel):
    service: str


class Greet(BaseModel):
    language: str


registry = Registry(
    "demo",
    "0.1.0",
    hub=MemoryHub(),
    session_store=MemorySessionStore(),
    instructions="Test server.",
)


@registry.tool
async def add(args: Add) -> Sum:
    """Add two integers."""
    return Sum(result=args.a + args.b)


@registry.tool
async def boom(args: Nothing) -> str:
    """Always raises."""
    raise RuntimeError("kaboom")


@registry.tool
async def confirm(args: Confirm, ex: Exchange) -> str:
    """MRTR round trip."""
    if (answer := ex.answers.get("confirm")) is not None:
        if answer.get("ok"):
            return f"deployed {ex.state['service']}"
        return "cancelled"
    if not ex.can_ask:
        return "cannot ask"
    raise NeedInput(
        {
            "confirm": elicit(
                f"Deploy {args.service}?",
                {"type": "object", "properties": {"ok": {"type": "boolean"}}},
            )
        },
        state={"service": args.service},
    )


@registry.tool
async def gated(args: Nothing, ex: Exchange) -> str:
    """Single confirmation through `ask`, reached over stdio."""
    confirm = await ex.ask("confirm", elicit("go ahead?"))
    if not confirm.accepted:
        return f"stopped at {confirm.action}"
    return "went ahead"


@registry.resource("config://app", mime_type="application/json")
async def config(args: Nothing) -> dict:
    """Fixed resource."""
    return {"debug": False}


@registry.resource("res://items/{id}", name="item")
async def item(args: ItemRef) -> str:
    """Templated resource."""
    return f"item-{args.id}"


@registry.prompt
async def greet(args: Greet) -> str:
    """Ask for a greeting."""
    return f"Hello, {args.language} speaker."


@registry.completions
async def complete(args: CompleteParams) -> list[str]:
    if args.argument.name == "language":
        return [x for x in ("python", "rust", "go") if x.startswith(args.argument.value)]
    return []


if __name__ == "__main__":
    asyncio.run(run_stdio(registry))
