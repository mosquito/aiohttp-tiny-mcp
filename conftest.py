"""Fixtures used by executable examples in the repository README."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest
from aiohttp import web
from pydantic import BaseModel

from aiohttp_tiny_mcp import Endpoint, Exchange, Registry, elicit
from aiohttp_tiny_mcp.hub import NOTIFICATIONS, topic
from aiohttp_tiny_mcp.testing import serving


class Deploy(BaseModel):
    service: str


class Nothing(BaseModel):
    pass


@pytest.fixture(name="__name__")
def module_name() -> str:
    return "docs.example"


@pytest.fixture
def registry() -> Registry:
    registry = Registry("readme", "0.1.0")

    @registry.tool
    async def deploy(args: Deploy, ex: Exchange) -> str:
        agreed = await ex.ask("confirm", elicit(f"Deploy {args.service}?"))
        return f"deployed {args.service}" if agreed.accepted else f"stopped at {agreed.action}"

    @registry.resource("config://app", mime_type="application/json")
    async def config(args: Nothing) -> dict:
        return {"debug": False}

    return registry


@pytest.fixture
def app(registry: Registry) -> web.Application:
    return Endpoint(registry).app("/mcp")


@pytest.fixture(name="url")
async def readme_url(registry: Registry) -> AsyncIterator[str]:
    async with serving(registry) as address:
        yield address


@pytest.fixture
async def readme_events(registry: Registry) -> AsyncIterator[None]:
    async def publish() -> None:
        while True:
            await registry.hub.publish(
                topic(NOTIFICATIONS),
                {
                    "jsonrpc": "2.0",
                    "method": "notifications/resources/updated",
                    "params": {"uri": "config://app"},
                },
            )
            await asyncio.sleep(0.02)

    task = asyncio.create_task(publish())
    try:
        yield
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
