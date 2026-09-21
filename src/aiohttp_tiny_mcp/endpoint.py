"""Streamable HTTP bound to an aiohttp router (docs/reference/runtime.md)."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Mapping
from dataclasses import replace
from functools import cached_property, partial
from typing import Any

from aiohttp import web

from .adapter import Adapter
from .auth import Authentication, Principal, Unauthorized
from .core import (
    Call,
    DecodeFailure,
    Failure,
    FailureKind,
    Operation,
    Outcome,
    Preamble,
    Rejected,
    Value,
)
from .dispatcher import Dispatcher
from .exchange import Exchange, is_reply, relay_reply
from .hub import NOTIFICATIONS, Subscription, topic
from .metadata import metadata_route
from .namespaces import current, namespace, scoped
from .protocol.selection import AdapterSet
from .registry import Registry
from .sessions import (
    SESSION_HEADER,
    Session,
    SessionRecord,
    handshake_data,
    new_session_id,
    stored_capabilities,
    stored_log_level,
    stored_owner,
    stored_version,
)
from .sse import SSEResponse
from .subscriptions import relays, wanted
from .tasks import stop

log = logging.getLogger("aiohttp_tiny_mcp")

MAY_ASK = frozenset({Operation.CALL_TOOL, Operation.GET_PROMPT, Operation.READ_RESOURCE})
AUTH_POLICY: web.RequestKey[Authentication] = web.RequestKey("mcp_auth_policy", Authentication)


def origin_pattern(spec: str) -> re.Pattern[str]:
    """Compile an allowed origin with wildcards.

    `*` matches one host label, `**` matches one or more labels, so
    `https://*.example.com` admits `https://app.example.com` and `https://**.example.com`
    admits `https://a.b.example.com` as well. Everything else matches literally.
    """
    parts = []
    for piece in re.split(r"(\*\*|\*)", spec):
        if piece == "**":
            parts.append(r"[^./]+(?:\.[^./]+)*")
        elif piece == "*":
            parts.append(r"[^./]+")
        else:
            parts.append(re.escape(piece))
    return re.compile("".join(parts))


class Endpoint:
    """The MCP endpoint, in the spec's sense: one path that accepts POST.

    Mount it however you mount anything else in aiohttp::

        app.add_routes(ep.routes("/mcp"))
        app.router.add_view("/mcp", ep.view)
        ep.setup(app, "/mcp")  # the same routes, added for you

    Under `add_subapp` the metadata route needs the root application, because
    a prefix must not reach a well-known path::

        section.add_routes(ep.routes("/mcp", metadata=False))
        app.add_subapp("/api/", section)
        app.add_routes(ep.metadata_routes())
    """

    def __init__(
        self,
        registry: Registry,
        *,
        adapters: AdapterSet | None = None,
        allowed_origins: set[str] | None = None,
        trust_proxy_origin_validation: bool = False,
        compress: bool = True,
    ) -> None:
        self.registry = registry
        self.adapters = adapters or AdapterSet.default()
        self.dispatcher = Dispatcher(registry)
        self.allowed_origins = allowed_origins
        self.origin_patterns = [origin_pattern(o) for o in allowed_origins or () if "*" in o]
        self.trust_proxy_origin_validation = trust_proxy_origin_validation
        self.compress = compress

    @cached_property
    def view(self) -> type[web.View]:
        """GET opens the legacy notification stream; POST handles requests.

        2026-07-28 uses subscriptions/listen instead of GET. DELETE returns 405; sessions end by
        expiration.
        """
        endpoint = self

        class MCPView(web.View):
            async def post(self) -> web.StreamResponse:
                return await endpoint.handle(self.request)

            async def get(self) -> web.StreamResponse:
                return await endpoint.notifications(self.request)

        return MCPView

    def routes(
        self, path: str = "/mcp", *, name: str | None = "mcp", metadata: bool = True
    ) -> list[web.RouteDef]:
        """The endpoint, and where a client looks to find out how to reach it.

        Metadata routes are included when a policy declares them. OAuth Bearer
        policies publish protected-resource metadata; Basic policies do not.
        Pass `metadata=False` when this application cannot serve those paths.
        See `metadata_routes`.
        """
        log.debug("MCP endpoint at %s, named %r", path, name)
        found = [web.route("*", path, self.view, name=name)]
        if metadata:
            found.extend(self.metadata_routes(name=name))
        return found

    def metadata_routes(self, *, name: str | None = "mcp") -> list[web.RouteDef]:
        """RFC 9728 metadata, for the application that owns the site root.

        Empty when no policy declares metadata. The path comes from the resource
        URL, and RFC 8615 puts a well-known URI directly under the authority,
        so a prefix must not reach it: an endpoint mounted with `add_subapp`
        takes `routes(metadata=False)` and leaves these to the root
        application.
        """
        found: list[web.RouteDef] = []
        documents: dict[str, dict[str, Any]] = {}
        for auth in self.registry.auth_policies:
            path = auth.metadata_path
            if path is None:
                continue
            document = auth.metadata()
            if path in documents:
                if documents[path] != document:
                    raise ValueError(f"conflicting authentication metadata at {path}")
                continue
            documents[path] = document
            suffix = f"-policy{len(found)}" if found else ""
            found.append(
                metadata_route(
                    web.get(
                        path,
                        partial(self.metadata, auth=auth),
                        name=f"{name}-resource-metadata{suffix}" if name else None,
                    )
                )
            )
        return found

    async def metadata(self, request: web.Request, *, auth: Authentication) -> web.Response:
        """RFC 9728: what this resource is and who issues tokens for it."""
        return web.json_response(auth.metadata(), headers={"Cache-Control": "public, max-age=3600"})

    async def verified(self, request: web.Request) -> Principal | None:
        """Return the verified caller, or None when no authentication is configured.

        Raises `Unauthorized`, which the caller turns into the refusal a
        client can act on. The namespace is set from what was verified, so
        every key this request touches is separated by an identity somebody
        checked rather than by a header the caller chose. An application that
        set its own namespace first keeps it.
        """
        policies = self.registry.auth_policies
        if not policies:
            return None
        refusals: list[Unauthorized] = []
        for auth in policies:
            try:
                principal = auth.check(await auth.authenticate(request))
            except Unauthorized as refusal:
                refusals.append(refusal)
                continue
            request[AUTH_POLICY] = auth
            if auth.namespace_from_token and current() is None:
                namespace.set(
                    principal.namespace if principal.namespace is not None else principal.identity
                )
            return principal
        # Preserve scope failures when another policy merely found no credentials.
        raise next((refusal for refusal in refusals if refusal.status == 403), refusals[-1])

    def refuse(self, refusal: Unauthorized) -> web.Response:
        return web.json_response(
            {"error": refusal.error, "error_description": refusal.description},
            status=refusal.status,
            headers={
                "WWW-Authenticate": ", ".join(
                    auth.challenge(refusal) for auth in self.registry.auth_policies
                )
            },
        )

    def setup(
        self, app: web.Application, path: str = "/mcp", *, name: str | None = "mcp"
    ) -> web.Application:
        log.debug("adding the MCP routes to %r", app)
        app[MCP_ENDPOINT] = self
        app.add_routes(self.routes(path, name=name))
        return app

    def app(self, path: str = "/mcp", **kw: Any) -> web.Application:
        return self.setup(web.Application(**kw), path)

    def check_origin(self, request: web.Request) -> None:
        origin = request.headers.get("Origin")
        if not origin or self.trust_proxy_origin_validation:
            return
        if origin == self.own_origin(request):
            return
        if self.allowed_origins is not None and origin in self.allowed_origins:
            return
        if any(pattern.fullmatch(origin) for pattern in self.origin_patterns):
            return
        raise Rejected(Failure(FailureKind.ORIGIN_REJECTED, "origin not allowed"))

    def own_origin(self, request: web.Request) -> str:
        """Origin of pages served by this endpoint. A rebound page retains the attacker's origin
        and fails this comparison.

        TLS-terminating proxies require trust_proxy_origin_validation to account for the
        external scheme.
        """
        host = request.headers.get("Host")
        return f"{request.scheme}://{host}" if host else ""

    def accepts(self, request: web.Request, media_type: str) -> bool:
        wanted_type, wanted_subtype = media_type.lower().split("/", 1)
        for value in request.headers.get("Accept", "*/*").split(","):
            media_range, *parameters = value.split(";")
            try:
                quality = next(
                    (
                        float(parameter.split("=", 1)[1])
                        for parameter in parameters
                        if parameter.strip().lower().startswith("q=")
                    ),
                    1.0,
                )
            except (ValueError, IndexError):
                quality = 0.0
            if quality <= 0:
                continue
            try:
                accepted_type, accepted_subtype = media_range.strip().lower().split("/", 1)
            except ValueError:
                continue
            if accepted_type in {"*", wanted_type} and accepted_subtype in {
                "*",
                wanted_subtype,
            }:
                return True
        return False

    def stream_reason(self, adapter: Adapter, call: Call) -> str | None:
        """Return the SSE requirement used in a 406 response, or None for JSON."""
        if call.is_notification:
            return None
        if call.operation is Operation.LISTEN:
            return "subscriptions/listen"
        if call.operation is Operation.CALL_TOOL:
            spec = self.registry.tools.get(call.target or "")
            if spec is not None and spec.streaming:
                return "streaming tool"
        if (
            call.operation in MAY_ASK
            and adapter.can_push_ask
            and call.client.capabilities.get("elicitation") is not None
        ):
            return "a question this revision would have to push"
        return None

    async def handle(self, request: web.Request) -> web.StreamResponse:
        try:
            self.check_origin(request)
        except Rejected as e:
            return self.render_failure(self.adapters.fallback(), e.failure)

        if request.content_type.lower() != "application/json":
            return web.Response(status=415, text="MCP requests require application/json")

        try:
            principal = await self.verified(request)
        except Unauthorized as refusal:
            assert self.registry.auth is not None
            return self.refuse(refusal)

        raw = await request.read()
        pre = Preamble.of(raw, request.headers, request.query)

        if is_reply(pre.body):
            await relay_reply(self.registry.hub, pre.body)
            return web.Response(status=202)

        try:
            session = await self.load_session(request, principal)
            adapter = self.adapters.select(pre, stored_version(session))
        except Rejected as e:
            return self.render_failure(self.adapters.fallback(), e.failure)
        held = self.open_values(request, session)

        log.debug("<- [%s] %s", adapter.version, raw.decode("utf-8", "replace"))

        try:
            items = adapter.decode(pre, self.registry)
            adapter.check_http(pre, request.headers, self.registry)
        except Rejected as e:
            return self.render_failure(adapter, e.failure)

        if session is not None:
            remembered = stored_capabilities(session)
            level = stored_log_level(session)
            for item in items:
                if isinstance(item, Call):
                    item.client = replace(
                        item.client,
                        capabilities={**remembered, **item.client.capabilities},
                    )
                    if item.log_level is None:
                        item.log_level = level

        if len(items) == 1 and isinstance(items[0], Call):
            call = items[0]
            streamed = self.stream_reason(adapter, call)
            if streamed is not None:
                if not self.accepts(request, "text/event-stream"):
                    return web.Response(status=406, text=f"{streamed} requires SSE")
                streaming = Exchange(self.registry, request, adapter, call, held)
                streaming.principal = principal
                return await self.stream(request, streaming)

        will_reply = any(
            item.must_respond if isinstance(item, DecodeFailure) else not item.is_notification
            for item in items
        )
        if will_reply and not self.accepts(request, "application/json"):
            return web.Response(status=406, text="client does not accept application/json")

        replies: list[tuple[int, Mapping[str, Any]]] = []
        minted: str | None = None
        for item in items:
            if isinstance(item, DecodeFailure):
                if item.must_respond:
                    replies.append(
                        (
                            adapter.http_status(item.failure),
                            adapter.encode_failure(item.id, item.failure),
                        )
                    )
                continue
            ex = Exchange(self.registry, request, adapter, item, held)
            ex.principal = principal
            outcome = await self.dispatcher.run(ex)
            if item.operation is Operation.DESCRIBE and session is None:
                owner = principal.identity if principal is not None else None
                minted = await self.open_session(adapter, item, outcome, owner)
            if item.is_notification:
                continue
            replies.append(self.encode(adapter, item, outcome))

        if not replies:
            return web.Response(status=202)
        if pre.is_batch:
            return web.Response(
                status=200,
                content_type="application/json",
                text=json.dumps([p for _, p in replies], ensure_ascii=False),
            )
        status, payload = replies[0]
        response = web.Response(
            status=status,
            content_type="application/json",
            text=json.dumps(payload, ensure_ascii=False),
        )
        if minted is not None:
            response.headers[SESSION_HEADER] = minted
        return response

    def stream_adapter(self, request: web.Request, session: SessionRecord | None) -> Adapter:
        """Select a GET stream revision from the session or protocol header."""
        version = stored_version(session) or request.headers.get("MCP-Protocol-Version")
        return self.adapters.resolve_version(version) if version else self.adapters.fallback()

    async def notifications(self, request: web.Request) -> web.StreamResponse:
        """Serve legacy notifications, re-reading subscriptions to include changes from other
        nodes.
        """
        try:
            self.check_origin(request)
        except Rejected as e:
            return self.render_failure(self.adapters.fallback(), e.failure)
        if not self.accepts(request, "text/event-stream"):
            return web.Response(status=406, text="this stream is text/event-stream")

        try:
            principal = await self.verified(request)
        except Unauthorized as refusal:
            assert self.registry.auth is not None
            return self.refuse(refusal)

        try:
            record = await self.load_session(request, principal)
        except Rejected as e:
            return self.render_failure(self.adapters.fallback(), e.failure)
        adapter = self.stream_adapter(request, record)
        if not adapter.has_handshake:
            return web.Response(
                status=405,
                headers={"Allow": "POST"},
                text=f"{adapter.version} reads notifications with subscriptions/listen",
            )

        response = SSEResponse(compress=self.compress)
        await response.prepare(request)
        relay = asyncio.create_task(self.relay_notifications(request, response, adapter))
        try:
            while not relay.done():
                await asyncio.wait({relay}, timeout=0.05)
                transport = request.transport
                if transport is None or transport.is_closing():
                    break
        finally:
            await stop(relay)
        return response

    async def relay_notifications(
        self, request: web.Request, response: SSEResponse, adapter: Adapter
    ) -> None:
        """Relay changes with their hub ids, re-reading subscriptions each pass."""
        capabilities = adapter.capabilities(self.registry)
        events = await self.resumed(request, topic(NOTIFICATIONS))
        while True:
            found = await events.poll()
            if not found:
                continue
            accepted = wanted(
                capabilities,
                self.open_values(request, await self.stored(request)),
                self.registry.broadcasts,
            )
            for event in found:
                if relays(event.message, accepted):
                    text = json.dumps(event.message, ensure_ascii=False)
                    log.debug("-> [%s] %s %s", adapter.version, event.id, text)
                    await response.send(text, id=event.id)

    async def resumed(self, request: web.Request, where: str) -> Subscription:
        """Subscribe from `Last-Event-ID`, or from now when it is absent or unknown."""
        hub = self.registry.hub
        wait = self.registry.hub_poll_seconds
        last = request.headers.get("Last-Event-ID")
        if last:
            try:
                return await hub.subscribe(where, after=last, wait=wait)
            except ValueError:
                log.debug("ignoring Last-Event-ID %r: not from this hub", last)
        return await hub.subscribe(where, wait=wait)

    def owns(
        self, record: SessionRecord | None, principal: Principal | None, request: web.Request
    ) -> bool:
        """Whether this caller may use this session.

        A session id travels in a header, so a copied one is a credential.
        The owner is compared through the store rather than through anything
        held in this process, which is what lets a session opened on one
        worker be used on another.
        """
        auth = request.get(AUTH_POLICY)
        if record is None or auth is None or not auth.bind_sessions:
            return True
        owner = stored_owner(record)
        if owner is None:
            return True
        return principal is not None and principal.identity == owner

    async def stored(self, request: web.Request) -> SessionRecord | None:
        """The record the session header names, or None where there is no header or no record."""
        session_id = request.headers.get(SESSION_HEADER)
        if not session_id:
            return None
        return await self.registry.session_store.get(scoped(session_id))

    async def load_session(self, request: web.Request, principal: Any) -> SessionRecord | None:
        """Load the legacy handshake this request names, or None without a session header.

        Raises `Rejected` where the header names a session the store does not
        hold or another principal owns. The spec answers that with 404 so the
        client opens a new session with `initialize` instead of going on with
        capabilities and a log level this server no longer remembers.

        A found session has its TTL renewed: it lives while the client keeps
        talking, not for a fixed time after the handshake.
        """
        session_id = request.headers.get(SESSION_HEADER)
        if not session_id:
            return None
        record = await self.registry.session_store.get(scoped(session_id))
        if record is None or not self.owns(record, principal, request):
            raise Rejected(Failure(FailureKind.SESSION_NOT_FOUND, "session not found"))
        await self.registry.session_store.touch(
            scoped(session_id), ttl_seconds=self.registry.session_ttl_seconds
        )
        return record

    def open_values(self, request: web.Request, record: SessionRecord | None) -> Session | None:
        """The application-owned half of this request's session, if any."""
        session_id = request.headers.get(SESSION_HEADER)
        if record is None or not session_id:
            return None
        return Session(
            self.registry.session_store, session_id, record, self.registry.session_ttl_seconds
        )

    async def open_session(
        self, adapter: Adapter, call: Call, outcome: Outcome, owner: str | None = None
    ) -> str | None:
        """Persist a successful legacy handshake when a store is configured."""
        store = self.registry.session_store
        if not adapter.has_handshake or not isinstance(outcome, Value):
            return None
        negotiated = getattr(outcome.result, "protocol_version", None)
        if not isinstance(negotiated, str):
            return None
        session_id = new_session_id()
        created = await store.create(
            scoped(session_id),
            handshake_data(negotiated, call.client.capabilities, owner),
            ttl_seconds=self.registry.session_ttl_seconds,
        )
        return session_id if created else None

    def encode(
        self, adapter: Adapter, call: Call, outcome: Outcome
    ) -> tuple[int, Mapping[str, Any]]:
        status = adapter.http_status(outcome) if isinstance(outcome, Failure) else 200
        payload = adapter.encode(call, self.registry, outcome)
        log.debug("-> [%s] %s %s", adapter.version, status, json.dumps(payload, ensure_ascii=False))
        return status, payload

    def render_failure(self, adapter: Adapter, failure: Failure) -> web.Response:
        payload = adapter.encode_failure(None, failure)
        text = json.dumps(payload, ensure_ascii=False)
        log.debug("-> [%s] %s %s", adapter.version, adapter.http_status(failure), text)
        return web.Response(
            status=adapter.http_status(failure), content_type="application/json", text=text
        )

    async def stream(self, request: web.Request, ex: Exchange) -> web.StreamResponse:
        """Stream request-scoped notifications and the final result."""
        sse = ex.open(compress=self.compress)
        await sse.prepare(request)
        outcome = await self.run_until_disconnect(
            request, ex, asyncio.create_task(self.dispatcher.run(ex))
        )
        if outcome is None:
            return sse
        try:
            _, payload = self.encode(ex.adapter, ex.call, outcome)
            await ex.emit(payload)
            await sse.write_eof()
        except (ConnectionResetError, ConnectionError):
            pass
        return sse

    async def run_until_disconnect(
        self, request: web.Request, ex: Exchange, task: asyncio.Task[Outcome]
    ) -> Outcome | None:
        """Cancel and join request work on disconnect or parent cancellation."""
        try:
            while not task.done():
                await asyncio.wait({task}, timeout=0.05)
                transport = request.transport
                if transport is None or transport.is_closing():
                    ex.cancel()
                    return None
            return task.result()
        except asyncio.CancelledError:
            ex.cancel()
            raise
        finally:
            await stop(task)


MCP_ENDPOINT = web.AppKey("mcp_endpoint", Endpoint)
