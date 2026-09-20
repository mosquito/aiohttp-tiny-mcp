"""Newline-delimited JSON-RPC over stdin/stdout. Send logs to stderr.

Modern requests select revisions per message; legacy clients retain their negotiated adapter.
In-flight tasks support subscriptions and cancellation. HTTP header checks belong to Endpoint.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import replace
from typing import Any

from .adapter import Adapter
from .core import (
    DecodeFailure,
    Failure,
    FailureKind,
    Preamble,
    Rejected,
)
from .dispatcher import Dispatcher
from .exchange import Exchange, is_reply, relay_reply
from .protocol.selection import AdapterSet
from .registry import Registry
from .tasks import stop

log = logging.getLogger("aiohttp_tiny_mcp")

Writer = Callable[[Mapping[str, Any]], Any]


class StdioRequest:
    """Request app access for AppKey-based dependency injection."""

    def __init__(self, app_state: dict[Any, Any] | None = None) -> None:
        self.app: dict[Any, Any] = app_state if app_state is not None else {}


async def serve_stdio(
    registry: Registry,
    reader: asyncio.StreamReader,
    write: Writer,
    *,
    adapters: AdapterSet | None = None,
    app_state: dict[Any, Any] | None = None,
) -> None:
    """Serve an injectable reader/writer pair, without requiring process stdin/stdout."""
    adapters = adapters or AdapterSet.default()
    dispatcher = Dispatcher(registry)
    request = StdioRequest(app_state)
    adapter: Adapter | None = None
    active: dict[str | int, tuple[Exchange, asyncio.Task[None]]] = {}
    declared: dict[str, Any] = {}
    level: list[str | None] = [None]

    def send(version: str, payload: Mapping[str, Any]) -> None:
        log.debug("-> [%s] %s", version, json.dumps(payload, ensure_ascii=False))
        write(payload)

    def keep_log_level(value: str) -> None:
        level[0] = value

    def attach(ex: Exchange) -> None:
        """Attach the notification channel before either execution path dispatches."""

        async def emit(payload: Mapping[str, Any]) -> None:
            send(ex.adapter.version, payload)

        ex.send = emit

    async def execute(ex: Exchange) -> None:
        attach(ex)
        try:
            outcome = await dispatcher.run(ex)
            if not ex.call.is_notification:
                await ex.emit(ex.adapter.encode(ex.call, registry, outcome))
        except asyncio.CancelledError:
            ex.cancel()
            raise
        except Exception:
            log.exception("stdio request failed")
        finally:
            if ex.call.id is not None:
                active.pop(ex.call.id, None)

    try:
        while True:
            raw = await reader.readline()
            if not raw:
                return
            raw = raw.strip()
            if not raw:
                continue

            log.debug("<- %s", raw.decode("utf-8", "replace"))
            pre = Preamble.of(raw, {})
            if is_reply(pre.body):
                await relay_reply(registry.hub, pre.body)
                continue
            body = pre.body
            if (
                isinstance(body, dict)
                and body.get("jsonrpc") == "2.0"
                and pre.method == "notifications/cancelled"
                and "id" not in body
            ):
                params = body.get("params")
                request_id = params.get("requestId") if isinstance(params, dict) else None
                if type(request_id) in (str, int) and request_id in active:
                    exchange, task = active[request_id]
                    exchange.cancel()
                    task.cancel()
                await asyncio.sleep(0)
                continue

            try:
                selected = adapter or adapters.select(pre)
                calls = selected.decode(pre, registry)
            except Rejected as e:
                fallback = adapters.fallback()
                send(fallback.version, fallback.encode_failure(None, e.failure))
                continue
            for item in calls:
                if isinstance(item, DecodeFailure):
                    if item.must_respond:
                        send(selected.version, selected.encode_failure(item.id, item.failure))
                    continue
                if selected.has_handshake:
                    declared.update(item.client.capabilities)
                    item.client = replace(item.client, capabilities=dict(declared))
                    if item.log_level is None:
                        item.log_level = level[0]
                if selected.has_handshake:
                    adapter = selected
                ex = Exchange(registry, request, selected, item)
                ex.keep_log_level = keep_log_level
                if not selected.can_ask and not ex.can_ask:
                    attach(ex)
                    outcome = await dispatcher.run(ex)
                    if not item.is_notification:
                        send(selected.version, selected.encode(item, registry, outcome))
                elif item.is_notification:
                    continue
                elif item.id in active:
                    failure = Failure(FailureKind.MALFORMED, "request id is already in flight")
                    send(selected.version, selected.encode_failure(item.id, failure))
                else:
                    assert item.id is not None
                    active[item.id] = (ex, asyncio.create_task(execute(ex)))
                    await asyncio.sleep(0)
    finally:
        tasks = []
        for exchange, task in list(active.values()):
            exchange.cancel()
            tasks.append(task)
        with suppress(Exception):
            await stop(*tasks)


async def stdin_reader() -> asyncio.StreamReader:
    loop = asyncio.get_event_loop()
    reader = asyncio.StreamReader()
    protocol = asyncio.StreamReaderProtocol(reader)
    await loop.connect_read_pipe(lambda: protocol, sys.stdin)
    return reader


def write_stdout(payload: Mapping[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


async def run_stdio(
    registry: Registry,
    *,
    adapters: AdapterSet | None = None,
    app_state: dict[Any, Any] | None = None,
) -> None:
    """Serve `registry` over real stdin/stdout until stdin closes."""
    reader = await stdin_reader()
    await serve_stdio(registry, reader, write_stdout, adapters=adapters, app_state=app_state)


__all__ = ["StdioRequest", "run_stdio", "serve_stdio"]
