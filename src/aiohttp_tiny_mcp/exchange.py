"""Per-request context: dependency resolution, progress, MRTR access."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
from collections.abc import Awaitable, Callable, Mapping
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from aiohttp import web

from .adapter import Adapter, RequestLike
from .auth import Principal
from .core import (
    Answer,
    AnswerAction,
    Call,
    ClientInfo,
    InputRequest,
    NeedInput,
    answer_of,
    logs_at,
)
from .hub import ASK, Hub, topic
from .sessions import Session, SessionAccess
from .sessions import new_session_id as new_id
from .sse import SSEResponse

if TYPE_CHECKING:
    from .registry import Registry

log = logging.getLogger("aiohttp_tiny_mcp")


def is_reply(body: Any) -> bool:
    """Pushed-question answers are JSON-RPC responses without a method."""
    return (
        isinstance(body, dict)
        and "method" not in body
        and ("result" in body or "error" in body)
        and isinstance(body.get("id"), str)
    )


async def relay_reply(hub: Hub, body: Mapping[str, Any]) -> None:
    """Publish an answer to its namespaced question topic, which may be read on another node."""
    reply = body["result"] if "result" in body else body["error"]
    await hub.publish(topic(ASK, str(body["id"])), {"reply": reply})


@dataclass(frozen=True, slots=True)
class Instance:
    """An existing dependency object registered with Registry.provide_instance."""

    value: Any


class Exchange:
    def __init__(
        self,
        registry: Registry,
        request: RequestLike,
        adapter: Adapter,
        call: Call,
        session: Session | None = None,
    ) -> None:
        self.registry = registry
        self.request = request
        self.adapter = adapter
        self.call = call
        #: Application values kept between calls, or None where this request
        #: reached no session.
        self.session = session
        self.principal: Principal | None = None
        self.keep_log_level: Callable[[str], None] | None = None
        self.sse: SSEResponse | None = None
        self.send: Callable[[Mapping[str, Any]], Awaitable[Any]] | None = None
        self.stack = AsyncExitStack()
        self.resolved: dict[type, Any] = {}
        self.cancelled = asyncio.Event()

    @asynccontextmanager
    async def scope(self):
        """Release per-call dependencies, propagating handler exceptions into providers for
        rollback.
        """
        async with self.stack:
            yield

    async def resolve(self, kind: type) -> Any:
        if kind is Exchange:
            return self
        if kind is Principal:
            return self.principal
        if kind in self.resolved:
            return self.resolved[kind]
        source = self.registry.providers.get(kind)
        if source is None:
            raise LookupError(f"no provider for {kind!r}")
        if isinstance(source, Instance):
            value = source.value
        elif isinstance(source, web.AppKey):
            value = self.request.app[source]
        elif inspect.isasyncgenfunction(source):
            value = await self.stack.enter_async_context(asynccontextmanager(source)(self))
        else:
            value = await source(self)
        self.resolved[kind] = value
        return value

    @property
    def id(self) -> str | int:
        assert self.call.id is not None
        return self.call.id

    @property
    def can_ask(self) -> bool:
        """Whether input can be requested: MRTR requires declared elicitation; the tool-argument
        fallback does not.
        """
        if self.adapter.asks_in_arguments:
            return True
        return self.asks_somehow and self.declared_elicitation

    @property
    def asks_somehow(self) -> bool:
        """Whether this revision can put a question to a client at all."""
        return self.adapter.can_ask or self.adapter.can_push_ask or self.adapter.asks_in_arguments

    @property
    def declared_elicitation(self) -> bool:
        """Capability presence is sufficient; an empty mapping is a valid declaration."""
        return self.call.client.capabilities.get("elicitation") is not None

    @property
    def sessions(self) -> SessionAccess:
        """Access sessions by explicit handles, including on revisions without protocol sessions."""
        return SessionAccess(self.registry.session_store, self.registry.session_ttl_seconds)

    @property
    def answers(self) -> dict[str, Any]:
        return dict(self.call.answers)

    def answered(self, key: str) -> bool:
        """Check for a reply, including accepted forms with empty content."""
        return key in self.call.actions or key in self.call.answers

    def accepted(self, key: str) -> bool:
        """Check the action; accepted and declined replies can both have empty content."""
        return self.call.actions.get(key) is AnswerAction.ACCEPT

    async def ask(
        self,
        key: str,
        request: InputRequest,
        *,
        default: Mapping[str, Any] | None = None,
    ) -> Answer:
        """Ask for input and return the answer.

        MRTR raises NeedInput and restarts the handler when the answer arrives, possibly on
        another node. Put irreversible work after the final ask; preceding work may run again.

        For clients that cannot be asked, `default` accepts an elicit_accept/decline/cancel
        result. Without a default, the call fails.
        """
        if self.answered(key):
            return Answer(
                action=self.call.actions.get(key, AnswerAction.ACCEPT),
                content=self.call.answers.get(key) or {},
            )
        if self.can_ask and self.adapter.can_push_ask:
            return await self.push_ask(key, request)
        if default is not None and not self.can_ask:
            return answer_of(default)
        raise NeedInput({key: request})

    async def push_ask(self, key: str, request: InputRequest) -> Answer:
        """Send a question on the active stream and wait through the hub.

        The reply may reach another node. Subscribe before sending so fast replies are not
        missed.
        """
        hub = self.registry.hub
        wire_id = new_id()
        where = topic(ASK, wire_id)
        replies = await hub.subscribe(where, wait=self.registry.hub_poll_seconds)
        await self.emit({"jsonrpc": "2.0", "id": wire_id, **request})
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.registry.ask_timeout_seconds
        try:
            while not self.cancelled.is_set():
                left = deadline - loop.time()
                if left <= 0:
                    return Answer(action=AnswerAction.CANCEL)
                for event in await replies.poll(timeout=min(left, replies.wait)):
                    reply = event.message.get("reply")
                    if isinstance(reply, Mapping):
                        return answer_of(reply)
            return Answer(action=AnswerAction.CANCEL)
        finally:
            await hub.delete(where)

    def action(self, key: str) -> AnswerAction | None:
        """Return ACCEPT, DECLINE, CANCEL, or None if unanswered."""
        return self.call.actions.get(key)

    @property
    def state(self) -> Any:
        return self.call.state

    @property
    def client_info(self) -> ClientInfo:
        return self.call.client

    @property
    def log_level(self) -> str | None:
        return self.call.log_level

    @property
    def progress_token(self) -> str | int:
        return self.call.progress_token if self.call.progress_token is not None else self.id

    async def emit(self, payload: Mapping[str, Any]) -> None:
        if self.cancelled.is_set():
            return
        if self.send is not None:
            await self.send(payload)
            return
        assert self.sse is not None
        text = json.dumps(payload, ensure_ascii=False)
        log.debug("-> [%s] %s", self.adapter.version, text)
        await self.sse.send(text)

    def cancel(self) -> None:
        self.cancelled.set()

    async def wait_cancelled(self) -> None:
        await self.cancelled.wait()

    def logs(self, level: str) -> bool:
        """Whether a message of `level` would reach this client."""
        return logs_at(self.log_level, level)

    async def log(self, level: str, data: Any, *, logger: str | None = None) -> None:
        """Emit a message at the requested severity on the current request stream. Without a
        stream, emit nothing.
        """
        if not self.logs(level):
            return
        params: dict[str, Any] = {"level": level, "data": data}
        if logger is not None:
            params["logger"] = logger
        await self.emit({"jsonrpc": "2.0", "method": "notifications/message", "params": params})

    async def progress(
        self, progress: float, total: float | None = None, message: str | None = None
    ) -> None:
        """Emit on the current HTTP response or stdio request."""
        if self.sse is None and self.send is None:
            return
        params: dict[str, Any] = {"progressToken": self.progress_token, "progress": progress}
        if total is not None:
            params["total"] = total
        if message is not None and self.adapter.progress_message:
            params["message"] = message
        await self.emit({"jsonrpc": "2.0", "method": "notifications/progress", "params": params})

    def open(self, *, compress: bool = True, **headers: str) -> SSEResponse:
        """The response this call streams on. Prepared by the caller."""
        self.sse = SSEResponse(compress=compress, headers=headers)
        return self.sse
