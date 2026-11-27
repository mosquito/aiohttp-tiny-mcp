"""The legacy 2024-11-05 HTTP+SSE transport."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Coroutine
from collections.abc import Set as AbstractSet
from dataclasses import replace
from typing import Any

from aiohttp import web
from yarl import URL

from aiohttp_tiny_mcp.access_log import install_token_filter
from aiohttp_tiny_mcp.auth import Principal, Unauthorized
from aiohttp_tiny_mcp.protocol.adapter import Adapter
from aiohttp_tiny_mcp.protocol.core import (
    Call,
    DecodeFailure,
    Failure,
    FailureKind,
    Operation,
    Preamble,
    Rejected,
    Value,
)
from aiohttp_tiny_mcp.protocol.models import serialized
from aiohttp_tiny_mcp.protocol.selection import AdapterSet
from aiohttp_tiny_mcp.storage.hub import NOTIFICATIONS, Subscription, topic
from aiohttp_tiny_mcp.storage.namespaces import scoped
from aiohttp_tiny_mcp.storage.sessions import (
    Session,
    SessionRecord,
    handshake_data,
    new_session_id,
    stored_capabilities,
    stored_log_level,
    stored_version,
)
from aiohttp_tiny_mcp.transport.sse import SSEResponse

from .batch import run_batch
from .dispatcher import Dispatcher
from .exchange import Exchange, is_reply, relay_reply
from .http import Endpoint
from .registry import Registry
from .resource_auth import allowed_resource
from .resource_tools import NATIVE, resource_capabilities, resource_modes
from .subscriptions import relays, wanted
from .tasks import stop

log = logging.getLogger(__name__)

STREAM = "sse"

VERSION = "2024-11-05"


class SseEndpoint:
    """Two endpoints: one to listen on, one to send to."""

    def __init__(
        self,
        registry: Registry,
        *,
        adapters: AdapterSet | None = None,
        allowed_origins: AbstractSet[str] | None = None,
        trust_proxy_origin_validation: bool = False,
        compress: bool = True,
        batch_concurrency: int = 4,
        sse_path: str = "/sse",
        message_path: str = "/messages",
    ) -> None:
        self.registry = registry
        self.adapters = adapters or AdapterSet.default()
        self.dispatcher = Dispatcher(registry)
        self.origins = Endpoint(
            registry,
            adapters=self.adapters,
            allowed_origins=allowed_origins,
            trust_proxy_origin_validation=trust_proxy_origin_validation,
            batch_concurrency=batch_concurrency,
        )
        self.compress = compress
        self.batch_concurrency = batch_concurrency
        self.sse_path = sse_path
        self.message_path = message_path
        #: The requests this endpoint still serves. Each one answered 202 already.
        self.running: set[asyncio.Task[None]] = set()

    @property
    def adapter(self) -> Adapter:
        """The one that renders a failure before a revision is known."""
        return self.adapters.by_version[VERSION]

    def speaking(self, pre: Preamble, record: SessionRecord) -> Adapter:
        """Select a handshake-capable revision."""
        chosen = self.adapters.select(pre, stored_version(record) or VERSION)
        if not chosen.has_handshake:
            raise Rejected(
                Failure(
                    FailureKind.UNSUPPORTED_VERSION,
                    f"{chosen.version} is not spoken over this transport",
                    data={
                        "supported": [a.version for a in self.adapters.adapters if a.has_handshake]
                    },
                )
            )
        return chosen

    async def listen(self, request: web.Request) -> web.StreamResponse:
        """Open a stream and send its POST address."""
        modes = resource_modes(self.registry, request)
        try:
            self.origins.check_origin(request)
            principal = await self.origins.verified(request)
        except Rejected as e:
            return self.origins.render_failure(self.adapter, e.failure)
        except Unauthorized as refusal:
            assert self.registry.auth is not None
            return self.origins.refuse(refusal)

        session_id = await self.open_session(principal.identity if principal is not None else None)
        where = topic(STREAM, session_id)
        hub = self.registry.hub
        wait = self.registry.hub_poll_seconds
        events = await hub.subscribe(where, wait=wait)
        shared = await hub.subscribe(topic(NOTIFICATIONS), wait=wait)

        response = SSEResponse(compress=self.compress)
        await response.prepare(request)
        posting = self.mounted(request) + self.message_path
        query = [("session_id", session_id)]
        if self.registry.resource_tools:
            query.extend(("resources", mode) for mode in request.query.getall("resources", []))
        await self.write(response, "endpoint", str(URL(posting).with_query(query)))

        try:
            await self.relay_until_disconnect(
                request,
                self.relay(response, events),
                self.relay_shared(response, shared, session_id, principal=principal, modes=modes),
            )
        finally:
            await hub.delete(where)
            await self.registry.session_store.delete(scoped(session_id))
        return response

    async def open_session(self, owner: str | None = None) -> str:
        """Create the short-lived session represented by an open stream."""
        session_id = new_session_id()
        await self.registry.session_store.create(
            scoped(session_id),
            handshake_data(VERSION, {}, owner),
            ttl_seconds=self.registry.session_ttl_seconds,
        )
        return session_id

    async def relay_until_disconnect(
        self, request: web.Request, *relays: Coroutine[Any, Any, None]
    ) -> None:
        """Run the relays until the client disconnects or one ends, then always join them."""
        tasks = [asyncio.create_task(relay) for relay in relays]
        try:
            while not any(task.done() for task in tasks):
                await asyncio.wait(tasks, timeout=0.05, return_when=asyncio.FIRST_COMPLETED)
                transport = request.transport
                if transport is None or transport.is_closing():
                    break
        finally:
            await stop(*tasks)

    async def relay(self, response: SSEResponse, events: Subscription) -> None:
        """Write everything published for this connection, until cancelled."""
        async for event in events:
            await self.write(response, "message", json.dumps(event.message, ensure_ascii=False))

    async def relay_shared(
        self,
        response: SSEResponse,
        events: Subscription,
        session_id: str,
        *,
        principal: Principal | None = None,
        modes: frozenset[str] = NATIVE,
    ) -> None:
        """Write the list changes, resource updates and broadcasts this session wants.

        The session is re-read for each batch, so a subscription made through
        the message endpoint takes effect on the stream that is already open.
        """
        store = self.registry.session_store
        while True:
            found = await events.poll()
            if not found:
                continue
            record = await store.get(scoped(session_id))
            if record is None:
                return
            adapter = self.adapters.resolve_version(stored_version(record) or VERSION)
            session = Session(store, session_id, record, self.registry.session_ttl_seconds)
            accepted = wanted(
                resource_capabilities(self.registry, adapter, modes),
                session,
                self.registry.broadcasts,
            )
            if "resourceSubscriptions" in accepted:
                accepted["resourceSubscriptions"] = [
                    uri
                    for uri in accepted["resourceSubscriptions"]
                    if allowed_resource(self.registry, adapter, uri, principal)
                ]
            for event in found:
                if relays(event.message, accepted):
                    text = serialized(event.message).decode()
                    await self.write(response, "message", text)

    async def write(self, response: SSEResponse, event: str, data: str) -> None:
        log.debug("-> [%s] %s %s", VERSION, event, data)
        await response.send(data, event=event)

    async def receive(self, request: web.Request) -> web.StreamResponse:
        """Take one message, answer 202, and serve it. The reply goes to the stream.

        The 202 does not wait for the handler. A client of this transport posts one
        message at a time, including the answer to a question a handler asked, so a
        handler that waits for that answer would wait for a message the client cannot
        send until this response arrives.

        The work retains request data, but not middleware resource lifetimes. Use
        handler-scoped providers for resources that require cleanup.
        """
        resource_modes(self.registry, request)
        try:
            self.origins.check_origin(request)
            principal = await self.origins.verified(request)
        except Rejected as e:
            return self.origins.render_failure(self.adapter, e.failure)
        except Unauthorized as refusal:
            assert self.registry.auth is not None
            return self.origins.refuse(refusal)

        session_id = request.query.get("session_id", "")
        record = await self.registry.session_store.get(scoped(session_id)) if session_id else None
        if record is None or not self.origins.owns(record, principal, request):
            return web.json_response({"error": "no such session"}, status=404)
        await self.registry.session_store.touch(
            scoped(session_id), ttl_seconds=self.registry.session_ttl_seconds
        )

        raw = await request.read()
        if log.isEnabledFor(logging.DEBUG):
            log.debug("<- [%s] %s", VERSION, raw.decode("utf-8", "replace"))
        where = topic(STREAM, session_id)

        pre = Preamble.of(raw, request.headers)

        # An answer to a pushed question is a response, so it has no method and no
        # handler. Relay it through the hub, which may wake a handler on another node.
        if is_reply(pre.body):
            await relay_reply(self.registry.hub, pre.body)
            return web.Response(status=202)

        try:
            adapter = self.speaking(pre, record)
            items = adapter.decode(pre)
        except Rejected as e:
            await self.registry.hub.publish(where, self.adapter.encode_failure(None, e.failure))
            return web.Response(status=202)

        async def execute(item: Call | DecodeFailure) -> None:
            await self.serve(
                adapter, item, session_id, record, where, request=request, principal=principal
            )

        async def work() -> None:
            await run_batch(items, execute, self.batch_concurrency if pre.is_batch else 1)

        # Answer 202 before the handler runs. A client of this transport posts one
        # message at a time, so a handler that waits for an answer would wait for a
        # message the client cannot send until this response arrives.
        task = asyncio.create_task(work())
        self.running.add(task)
        task.add_done_callback(self.finished)
        return web.Response(status=202)

    def finished(self, task: asyncio.Task[None]) -> None:
        """Forget a finished request, and report what no caller can see."""
        self.running.discard(task)
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            log.exception("a message on the HTTP+SSE transport failed", exc_info=error)

    async def closing(self, app: web.Application) -> None:
        """Signal handler shape of `aclose`, registered by `setup`."""
        await self.aclose()

    async def aclose(self) -> None:
        """End the requests still in flight. Call this where `setup` was not used."""
        await stop(*self.running)
        self.running.clear()

    async def serve(
        self,
        adapter: Adapter,
        item: Call | DecodeFailure,
        session_id: str,
        record: SessionRecord,
        where: str,
        *,
        request: web.Request,
        principal: Principal | None,
    ) -> None:
        hub = self.registry.hub
        if isinstance(item, DecodeFailure):
            if item.must_respond:
                await hub.publish(where, adapter.encode_failure(item.id, item.failure))
            return

        item.client = replace(
            item.client,
            capabilities={**stored_capabilities(record), **item.client.capabilities},
        )
        if item.log_level is None:
            item.log_level = stored_log_level(record)

        session = Session(
            self.registry.session_store, session_id, record, self.registry.session_ttl_seconds
        )
        ex = Exchange(self.registry, request, adapter, item, session=session)
        ex.principal = principal
        ex.send = lambda payload: hub.publish(where, payload)

        try:
            outcome = await self.dispatcher.run(ex)
        except Exception as e:  # noqa: BLE001 -- a failed call must still answer
            log.exception("sse request failed")
            outcome = Failure(FailureKind.INTERNAL, f"{type(e).__name__}: {e}")

        if item.operation is Operation.DESCRIBE and isinstance(outcome, Value):
            negotiated = getattr(outcome.result, "protocol_version", None)
            if isinstance(negotiated, str):
                await session.remember_version(negotiated)
            # This transport opens the session before the handshake, so the capabilities
            # arrive later than the record. Without them the server reads a client that
            # declared nothing, and refuses to ask it anything.
            await session.remember_capabilities(item.client.capabilities)

        if item.is_notification:
            return
        await hub.publish(where, adapter.encode(item, self.registry, outcome))

    def mounted(self, request: web.Request) -> str:
        """The prefix the client reached this stream through.

        A subapplication adds its prefix to the request path and not to the
        route, and the address on the stream is one a client posts to, so it
        has to carry that prefix.
        """
        path = request.path
        return path[: -len(self.sse_path)] if path.endswith(self.sse_path) else ""

    def routes(
        self,
        sse_path: str | None = None,
        message_path: str | None = None,
        *,
        metadata: bool = True,
    ) -> list[web.RouteDef]:
        """One route to listen on, one to post to.

        Either path may be set here or on the constructor. Both are kept,
        because the stream names the posting path to the client.

        Policies that declare metadata include its routes. Use `metadata=False`
        when another endpoint serves it or when mounting under a subapplication.
        In a subapplication, add `metadata_routes()` to the root application.
        """
        if sse_path is not None:
            self.sse_path = sse_path
        if message_path is not None:
            self.message_path = message_path
        log.debug("HTTP+SSE stream at %s, messages at %s", self.sse_path, self.message_path)
        found = [
            web.get(self.sse_path, self.listen),
            web.post(self.message_path, self.receive),
        ]
        if metadata:
            found.extend(self.metadata_routes())
        return found

    def metadata_routes(self, *, name: str | None = "mcp-sse") -> list[web.RouteDef]:
        """Return protected-resource metadata routes for the root application."""
        return self.origins.metadata_routes(name=name)

    def setup(
        self,
        app: web.Application,
        sse_path: str | None = None,
        message_path: str | None = None,
        *,
        metadata: bool = True,
        mask_tokens_in_logs: bool = True,
    ) -> web.Application:
        if mask_tokens_in_logs:
            install_token_filter()
        log.debug("adding the HTTP+SSE routes to %r", app)
        app.add_routes(self.routes(sse_path, message_path, metadata=metadata))
        app.on_shutdown.append(self.closing)
        return app


__all__ = ["STREAM", "VERSION", "SseEndpoint"]
