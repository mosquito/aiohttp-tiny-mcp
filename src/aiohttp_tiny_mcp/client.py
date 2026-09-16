"""HTTP transport for BaseClient. See stdio_client.py for the stdio transport."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable, Mapping
from typing import Any

import aiohttp

from .adapter import Adapter
from .client_base import BaseClient, Elicitor
from .models import Implementation
from .sse import read_sse

ANSWER_METHOD = "elicitation/create"

NOTIFICATIONS_METHOD = "notifications/message"

BASE_HEADERS = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}


async def frames(resp: aiohttp.ClientResponse) -> AsyncIterator[dict[str, Any]]:
    """Yield JSON or SSE messages immediately; waiting for EOF would deadlock pushed questions."""
    if resp.content_type != "text/event-stream":
        yield await resp.json(content_type=None)
        return
    async for event in read_sse(resp):
        if event.data:
            yield json.loads(event.data)


class Client(BaseClient):
    def __init__(
        self,
        base_url: str,
        adapter: Adapter,
        *,
        client_info: Implementation | None = None,
        session: aiohttp.ClientSession | None = None,
        on_ask: Elicitor | None = None,
        on_notification: Callable[[Mapping[str, Any]], Any] | None = None,
        log_level: str | None = None,
    ) -> None:
        super().__init__(
            adapter,
            client_info=client_info,
            on_ask=on_ask,
            on_notification=on_notification,
            log_level=log_level,
        )
        self.base_url = base_url
        self.session = session
        self.owns_session = session is None
        self.session_id: str | None = None

    async def __aenter__(self) -> Client:
        if self.owns_session:
            self.session = aiohttp.ClientSession()
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self.owns_session and self.session is not None:
            await self.session.close()

    def headers(
        self, method: str, name: str | None = None, params: Any = None, tool: Any = None
    ) -> dict[str, str]:
        headers = {
            **BASE_HEADERS,
            **self.adapter.client_headers(method, name, params, tool),
        }
        if self.session_id is not None:
            headers["Mcp-Session-Id"] = self.session_id
        return headers

    async def exchange(
        self, envelope: dict[str, Any], *, method: str, name: str | None
    ) -> AsyncIterator[dict[str, Any]]:
        assert self.session is not None, "use 'async with Client(...) as client:'"
        tool = self.tool_definitions.get(name) if name is not None else None
        headers = self.headers(method, name, envelope.get("params"), tool)
        async with self.session.post(self.base_url, json=envelope, headers=headers) as resp:
            issued = resp.headers.get("Mcp-Session-Id")
            if issued:
                self.session_id = issued
            async for frame in frames(resp):
                yield frame

    async def send_notification(self, envelope: dict[str, Any], *, method: str) -> None:
        assert self.session is not None, "use 'async with Client(...) as client:'"
        headers = self.headers(method, params=envelope.get("params"))
        async with self.session.post(self.base_url, json=envelope, headers=headers) as resp:
            await resp.read()

    async def stream_notifications(self) -> AsyncIterator[dict[str, Any]]:
        """Open the legacy notification stream with GET; keep it open until cancelled."""
        assert self.session is not None, "use 'async with Client(...) as client:'"
        headers = self.headers(NOTIFICATIONS_METHOD)
        headers["Accept"] = "text/event-stream"
        async with self.session.get(self.base_url, headers=headers) as resp:
            resp.raise_for_status()
            async for frame in frames(resp):
                yield frame

    async def reply(self, envelope: dict[str, Any]) -> None:
        """Send a bare JSON-RPC response. The hub routes it to the node waiting for the answer."""
        assert self.session is not None, "use 'async with Client(...) as client:'"
        headers = self.headers(ANSWER_METHOD)
        async with self.session.post(self.base_url, json=envelope, headers=headers) as resp:
            await resp.read()
