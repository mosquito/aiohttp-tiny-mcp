import asyncio
import json
import os

import aiohttp
import pytest


class OldClient:
    """A 2024-11-05 client, in as few lines as the transport allows."""

    def __init__(self, base: str, *, headers: dict[str, str] | None = None) -> None:
        self.base = base
        self.http = aiohttp.ClientSession(headers=headers)
        self.stream = None
        self.post_to = ""
        self.seen: asyncio.Queue = asyncio.Queue()
        self.reading: asyncio.Task | None = None

    async def open(self) -> str:
        self.stream = await self.http.get(
            f"{self.base}/sse", headers={"Accept": "text/event-stream"}
        )
        event, data = await self.next_event()
        assert event == "endpoint", f"the first event must be `endpoint`, not {event}"
        self.post_to = data
        self.reading = asyncio.ensure_future(self.read())
        return data

    async def next_event(self) -> tuple[str, str]:
        event = ""
        async for raw in self.stream.content:
            line = raw.decode().strip()
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:"):
                return event, line[5:].strip()
        raise AssertionError("the stream ended")

    async def read(self) -> None:
        while True:
            event, data = await self.next_event()
            await self.seen.put(json.loads(data))

    async def send(self, method: str, params: dict, request_id: int | None = 1) -> None:
        envelope = {"jsonrpc": "2.0", "method": method, "params": params}
        if request_id is not None:
            envelope["id"] = request_id
        async with self.http.post(f"{self.base}{self.post_to}", json=envelope) as response:
            assert response.status == 202, await response.text()

    async def call(self, method: str, params: dict, request_id: int = 1) -> dict:
        await self.send(method, params, request_id)
        while True:
            message = await asyncio.wait_for(self.seen.get(), 5)
            if message.get("id") == request_id:
                return message

    async def close(self) -> None:
        if self.reading is not None:
            self.reading.cancel()
        if self.stream is not None:
            self.stream.close()
        await self.http.close()


REQUIRED = os.environ.get("MCP_REQUIRE_BACKENDS") == "1"


REDIS_URL = os.environ.get("REDIS_URL", "redis://127.0.0.1:6379")


POSTGRES_URL = os.environ.get("POSTGRES_URL", "postgresql://mcp:mcp@127.0.0.1:5432/mcp")


def missing(what: str, url: str):
    """Skip, or fail where the backend was promised."""
    message = f"no {what} at {url}"
    if REQUIRED:
        pytest.fail(message)
    pytest.skip(message)
