"""Persist authenticated MCP activity and expose an administrative audit feed."""

from __future__ import annotations

import asyncio
import re
from typing import Any

from aiohttp import web
from aiohttp_tiny_mcp.protocol.core import Failure, Operation, Outcome, Value
from aiohttp_tiny_mcp.protocol.models import CallToolResult, TextContent
from aiohttp_tiny_mcp.server.dispatcher import Dispatcher
from aiohttp_tiny_mcp.server.exchange import Exchange
from aiohttp_tiny_mcp.server.registry import Registry

from .auth import NO_STORE, require_admin, validated_org, validated_user
from .store import Store

SENSITIVE_KEYS = {"secret", "jwt_secret", "token", "access_token", "refresh_token", "authorization"}


class AuditFeed:
    def __init__(self) -> None:
        self.listeners: set[tuple[str, str | None, asyncio.Queue[None]]] = set()
        self.closed = False

    def publish(self, org: str, user: str) -> None:
        for selected_org, selected_user, queue in self.listeners:
            if selected_org == org and (selected_user is None or selected_user == user):
                if queue.empty():
                    queue.put_nowait(None)

    async def close(self, app: web.Application) -> None:
        self.closed = True
        for listener in self.listeners:
            queue = listener[2]
            if queue.empty():
                queue.put_nowait(None)


def sensitive_values(value: Any) -> list[str]:
    if isinstance(value, dict):
        return [
            secret
            for key, item in value.items()
            for secret in ([str(item)] if key.lower() in SENSITIVE_KEYS else sensitive_values(item))
            if secret
        ]
    if isinstance(value, list):
        return [secret for item in value for secret in sensitive_values(item)]
    return []


def redact(value: Any, secrets: list[str]) -> Any:
    if isinstance(value, dict):
        return {
            key: "[redacted]" if key.lower() in SENSITIVE_KEYS else redact(item, secrets)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item, secrets) for item in value]
    if isinstance(value, str):
        for secret in secrets:
            value = value.replace(secret, "[redacted]")
        return re.sub(r"(?i)([?&]token=)[^&\s]+", r"\1[redacted]", value)
    return value


class ObservedDispatcher(Dispatcher):
    def __init__(self, registry: Registry, store: Store) -> None:
        super().__init__(registry)
        self.store = store
        self.feed = AuditFeed()

    async def run(self, ex: Exchange) -> Outcome:
        outcome = await super().run(ex)
        principal = ex.principal
        if (
            ex.call.operation not in {Operation.CALL_TOOL, Operation.READ_RESOURCE}
            or principal is None
            or principal.namespace is None
            or principal.subject is None
        ):
            return outcome
        arguments = dict(ex.call.arguments)
        tool = ex.call.target or ""
        if ex.call.operation == Operation.READ_RESOURCE:
            arguments = {"uri": ex.call.target}
            tool = "resources/read"
        result: Any = None
        error: str | None = None
        if isinstance(outcome, Value):
            result = outcome.result.model_dump(mode="json", by_alias=True, exclude_none=True)
            if isinstance(outcome.result, CallToolResult) and outcome.result.is_error:
                error = "\n".join(
                    item.text for item in outcome.result.content if isinstance(item, TextContent)
                )
        elif isinstance(outcome, Failure):
            error = outcome.message
            result = {"kind": outcome.kind.value, "message": outcome.message, "data": outcome.data}
        secrets = sensitive_values(arguments)
        await self.store.record_event(
            principal.namespace,
            principal.subject,
            tool,
            redact(arguments, secrets),
            redact(result, secrets),
            redact(error, secrets),
        )
        self.feed.publish(principal.namespace, principal.subject)
        return outcome


async def stream(
    request: web.Request,
    feed: AuditFeed,
    store: Store,
    secret: str | None,
    org: str,
    user: str | None,
    tools: list[str] | None = None,
) -> web.StreamResponse:
    response = web.StreamResponse(
        headers={
            **NO_STORE,
            "Content-Type": "text/event-stream",
            "X-Accel-Buffering": "no",
        }
    )
    queue: asyncio.Queue[None] = asyncio.Queue(maxsize=1)
    listener = (org, user, queue)
    feed.listeners.add(listener)
    try:
        latest = await store.latest_event(org, user, tools=tools)
        await response.prepare(request)
        await response.write(b"event: ready\ndata: {}\n\n")
        heartbeat = asyncio.get_running_loop().time()
        while not feed.closed:
            try:
                await asyncio.wait_for(queue.get(), timeout=1)
            except asyncio.TimeoutError:
                pass
            if feed.closed:
                break
            try:
                await require_admin(request, secret)
            except web.HTTPException:
                break
            current = await store.latest_event(org, user, tools=tools)
            now = asyncio.get_running_loop().time()
            if current != latest:
                await response.write(b"event: audit\ndata: {}\n\n")
                latest = current
                heartbeat = now
            elif now - heartbeat >= 15:
                await response.write(b": heartbeat\n\n")
                heartbeat = now
    except (ConnectionError, RuntimeError):
        pass
    finally:
        feed.listeners.discard(listener)
    return response


def setup(app: web.Application, store: Store, secret: str | None, feed: AuditFeed) -> None:
    async def observe(request: web.Request) -> web.StreamResponse:
        await require_admin(request, secret)
        try:
            if any(key != "tool" and len(request.query.getall(key)) != 1 for key in request.query):
                raise ValueError("Supply each query parameter other than tool only once.")
            requested_tools = request.query.getall("tool", [])
            if len(requested_tools) > 32 or any(
                len(name) > 128 or not re.fullmatch(r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*", name)
                for name in requested_tools
            ):
                raise ValueError("Supply at most 32 tool names of 1–128 valid characters each.")
            tools = list(dict.fromkeys(requested_tools)) if requested_tools else None
            org = validated_org(request.query.get("org", "example.com"))
            user = validated_user(request.query["user"]) if "user" in request.query else None
            if request.query.get("stream", "false") not in {"true", "false"}:
                raise ValueError("stream must be true or false.")
            numbers: dict[str, int] = {}
            for key in ("before", "limit"):
                if key in request.query:
                    raw = request.query[key]
                    if not re.fullmatch(r"[0-9]{1,19}", raw):
                        raise ValueError(f"{key} must be a positive integer.")
                    numbers[key] = int(raw)
                    if not 1 <= numbers[key] <= 9223372036854775807:
                        raise ValueError(f"{key} must be a positive SQLite integer.")
            if not 1 <= numbers.get("limit", 50) <= 100:
                raise ValueError("limit must be between 1 and 100.")
            if request.query.get("stream") == "true" or "text/event-stream" in request.headers.get(
                "Accept", ""
            ):
                return await stream(request, feed, store, secret, org, user, tools)
            result = await store.observe(
                org, user, numbers.get("before"), numbers.get("limit", 50), tools=tools
            )
        except ValueError as error:
            raise web.HTTPBadRequest(text=str(error), headers=NO_STORE) from None
        return web.json_response(result, headers=NO_STORE)

    app.router.add_get("/admin/observe", observe)
    app.on_shutdown.append(feed.close)
