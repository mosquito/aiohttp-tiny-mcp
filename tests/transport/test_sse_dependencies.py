from __future__ import annotations

import asyncio

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from pydantic import BaseModel

from aiohttp_tiny_mcp import Exchange, Registry, SseEndpoint
from tests.helpers import OldClient

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(10)]


class Nothing(BaseModel):
    pass


class Connection:
    open = True


async def test_a_provider_outlives_the_202_response_and_closes_after_the_handler():
    registry = Registry("lifetime", "1.0")
    active = web.RequestKey("active", dict)
    started, release, closed = asyncio.Event(), asyncio.Event(), asyncio.Event()

    @web.middleware
    async def middleware(request, handler):
        request[active] = {"open": True}
        try:
            return await handler(request)
        finally:
            request[active]["open"] = False

    async def connection(ex):
        value = Connection()
        try:
            yield value
        finally:
            value.open = False
            closed.set()

    registry.provide(Connection, connection)

    @registry.tool
    async def waiting(args: Nothing, ex: Exchange, value: Connection) -> str:
        assert not ex.request[active]["open"]
        started.set()
        await release.wait()
        assert value.open
        return "ready"

    app = web.Application(middlewares=[middleware])
    SseEndpoint(registry).setup(app)
    async with TestServer(app) as server:
        client = OldClient(str(server.make_url("")))
        try:
            await client.open()
            await client.call("initialize", {"protocolVersion": "2024-11-05"})
            await client.send("tools/call", {"name": "waiting", "arguments": {}}, request_id=2)
            await asyncio.wait_for(started.wait(), 2)
            assert not closed.is_set()
            release.set()
            reply = await asyncio.wait_for(client.seen.get(), 2)
            assert reply["result"]["content"][0]["text"] == "ready"
            assert closed.is_set()
        finally:
            release.set()
            await client.close()
