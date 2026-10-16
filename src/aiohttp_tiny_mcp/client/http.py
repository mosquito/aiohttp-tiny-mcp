"""HTTP transport for BaseClient. The stdio transport lives in client.stdio."""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from typing import Any

import aiohttp

from aiohttp_tiny_mcp.protocol.adapter import Adapter
from aiohttp_tiny_mcp.protocol.models import Implementation
from aiohttp_tiny_mcp.transport.sse import read_sse

from .base import BaseClient, Elicitor
from .oauth import OAuth, OAuthError

ANSWER_METHOD = "elicitation/create"

NOTIFICATIONS_METHOD = "notifications/message"

BASE_HEADERS = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}

log = logging.getLogger(__name__)


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
        auth: OAuth | None = None,
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
        self.auth = auth
        if auth is not None:
            auth.check_endpoint(base_url)
        #: The id of the last event read from the notification stream.
        self.last_event_id: str | None = None
        self.session_id: str | None = None

    async def __aenter__(self) -> Client:
        self.check_auth()
        if self.owns_session:
            self.session = aiohttp.ClientSession()
        return self

    def check_auth(self) -> None:
        if self.auth is not None:
            self.auth.check_endpoint(self.base_url)
        if self.auth is not None and self.session is not None:
            if "Authorization" in self.session.headers or self.session.auth is not None:
                raise ValueError("OAuth cannot be combined with session Authorization or auth.")

    @asynccontextmanager
    async def http_request(
        self, method: str, *, headers: dict[str, str], **kwargs: Any
    ) -> AsyncIterator[aiohttp.ClientResponse]:
        assert self.session is not None, "use 'async with Client(...) as client:'"
        if self.auth is None:
            async with self.session.request(
                method, self.base_url, headers=headers, **kwargs
            ) as resp:
                yield resp
            return
        self.check_auth()
        token = await self.auth.token()
        for attempt in range(2):
            async with self.session.request(
                method,
                self.base_url,
                headers={**headers, "Authorization": f"Bearer {token}"},
                allow_redirects=False,
                **kwargs,
            ) as resp:
                if 300 <= resp.status < 400:
                    raise OAuthError("Authenticated MCP requests must not follow redirects.")
                if resp.status in {401, 403}:
                    challenge = self.auth.observe(resp.headers.getall("WWW-Authenticate", []))
                    if resp.status == 403:
                        if challenge.get("error") != "insufficient_scope":
                            raise OAuthError("The MCP server denied access (HTTP 403).")
                        raise self.auth.authorization_required()
                    if attempt:
                        await self.auth.invalidate(token)
                        raise self.auth.authorization_required()
                else:
                    yield resp
                    return
            # Release the response before refreshing, including with a one-slot connector.
            token = await self.auth.token(rejected=token)

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

    def lost_session(self, resp: aiohttp.ClientResponse) -> bool:
        """Whether the server no longer holds the session this request named.

        The spec answers such a request with 404. A 404 without a session id
        in flight is an ordinary reply and is read as one.
        """
        return resp.status == 404 and self.session_id is not None

    async def reopen(self) -> None:
        """Start over with initialize, which mints a session and restores the log level."""
        log.debug("session %s is gone, opening a new one", self.session_id)
        self.session_id = None
        await self.initialize()

    async def exchange(
        self, envelope: dict[str, Any], *, method: str, name: str | None
    ) -> AsyncIterator[dict[str, Any]]:
        assert self.session is not None, "use 'async with Client(...) as client:'"
        tool = self.tool_definitions.get(name) if name is not None else None
        params = envelope.get("params")
        async with self.http_request(
            "POST", json=envelope, headers=self.headers(method, name, params, tool)
        ) as resp:
            if not self.lost_session(resp):
                self.adopt(resp)
                async for frame in frames(resp):
                    yield frame
                return
        # Retry once on a fresh session; a second 404 is reported as any other reply.
        await self.reopen()
        async with self.http_request(
            "POST", json=envelope, headers=self.headers(method, name, params, tool)
        ) as resp:
            self.adopt(resp)
            async for frame in frames(resp):
                yield frame

    def adopt(self, resp: aiohttp.ClientResponse) -> None:
        """Keep the session id a handshake reply carries."""
        issued = resp.headers.get("Mcp-Session-Id")
        if issued:
            self.session_id = issued

    async def send_notification(self, envelope: dict[str, Any], *, method: str) -> None:
        assert self.session is not None, "use 'async with Client(...) as client:'"
        params = envelope.get("params")
        async with self.http_request(
            "POST", json=envelope, headers=self.headers(method, params=params)
        ) as resp:
            if not self.lost_session(resp):
                await resp.read()
                return
        await self.reopen()
        async with self.http_request(
            "POST", json=envelope, headers=self.headers(method, params=params)
        ) as resp:
            await resp.read()

    async def stream_notifications(
        self, *, last_event_id: str | None = None
    ) -> AsyncIterator[dict[str, Any]]:
        """Read the legacy GET stream until cancelled. `last_event_id` asks for a replay."""
        assert self.session is not None, "use 'async with Client(...) as client:'"
        headers = self.headers(NOTIFICATIONS_METHOD)
        headers["Accept"] = "text/event-stream"
        if last_event_id is not None:
            headers["Last-Event-ID"] = last_event_id
        async with self.http_request("GET", headers=headers) as resp:
            resp.raise_for_status()
            async for event in read_sse(resp):
                if event.id is not None:
                    self.last_event_id = event.id
                if event.data:
                    yield json.loads(event.data)

    async def reply(self, envelope: dict[str, Any]) -> None:
        """Send a bare JSON-RPC response. The hub routes it to the node waiting for the answer."""
        assert self.session is not None, "use 'async with Client(...) as client:'"
        headers = self.headers(ANSWER_METHOD)
        async with self.http_request("POST", json=envelope, headers=headers) as resp:
            await resp.read()
