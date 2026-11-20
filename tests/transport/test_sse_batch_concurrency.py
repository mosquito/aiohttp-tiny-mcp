"""Legacy HTTP+SSE batches can overlap independent calls when configured."""

import asyncio

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from pydantic import BaseModel

from aiohttp_tiny_mcp import Registry, SseEndpoint
from tests.helpers import OldClient

pytestmark = pytest.mark.timeout(10)


class Item(BaseModel):
    number: int


@pytest.mark.parametrize("concurrency", [None, 1, 2])
async def test_sse_batch_execution_and_reply_order(concurrency: int | None):
    concurrent = concurrency != 1
    registry = Registry("batches", "1")
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    events: list[tuple[str, int]] = []

    @registry.tool
    async def work(args: Item) -> str:
        events.append(("start", args.number))
        if args.number == 1:
            first_started.set()
            await release_first.wait()
        events.append(("finish", args.number))
        return str(args.number)

    endpoint = (
        SseEndpoint(registry)
        if concurrency is None
        else SseEndpoint(registry, batch_concurrency=concurrency)
    )
    async with TestServer(endpoint.setup(web.Application())) as server:
        client = OldClient(str(server.make_url("")))
        sending: asyncio.Task | None = None
        try:
            await client.open()
            initialized = await client.call(
                "initialize",
                {
                    "protocolVersion": "2025-03-26",
                    "clientInfo": {"name": "batch-test", "version": "1"},
                    "capabilities": {},
                },
                request_id=10,
            )
            assert initialized["result"]["protocolVersion"] == "2025-03-26"

            async def send_batch() -> int:
                async with client.http.post(
                    f"{client.base}{client.post_to}",
                    json=[
                        {
                            "jsonrpc": "2.0",
                            "id": number,
                            "method": "tools/call",
                            "params": {"name": "work", "arguments": {"number": number}},
                        }
                        for number in (1, 2)
                    ],
                ) as response:
                    return response.status

            sending = asyncio.create_task(send_batch())
            await asyncio.wait_for(first_started.wait(), 5)
            replies = []
            if concurrent:
                replies.append(await asyncio.wait_for(client.seen.get(), 5))
                assert replies[0]["id"] == 2
                assert not sending.done()
                assert events == [("start", 1), ("start", 2), ("finish", 2)]
            release_first.set()
            assert await sending == 202
            while len(replies) < 2:
                replies.append(await asyncio.wait_for(client.seen.get(), 5))
            assert [reply["id"] for reply in replies] == ([2, 1] if concurrent else [1, 2])
            assert all(
                reply["result"]["content"][0]["text"] == str(reply["id"]) for reply in replies
            )
            if not concurrent:
                assert events == [("start", 1), ("finish", 1), ("start", 2), ("finish", 2)]
        finally:
            release_first.set()
            if sending is not None:
                await asyncio.gather(sending, return_exceptions=True)
            await client.close()
