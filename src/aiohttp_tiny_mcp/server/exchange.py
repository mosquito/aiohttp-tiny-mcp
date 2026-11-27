"""Per-request context: dependency resolution, progress, MRTR access."""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from aiohttp import web

from aiohttp_tiny_mcp.auth import Principal
from aiohttp_tiny_mcp.protocol.adapter import Adapter, RequestLike
from aiohttp_tiny_mcp.protocol.core import (
    COMPLETION_KEY,
    NEEDED_CAPABILITY,
    Answer,
    AnswerAction,
    Call,
    ClientInfo,
    Failure,
    FailureKind,
    InputRequest,
    NeedInput,
    Rejected,
    answer_of,
    logs_at,
)
from aiohttp_tiny_mcp.protocol.models import (
    CompletionMessage,
    CompletionRequest,
    CompletionResult,
    serialized,
)
from aiohttp_tiny_mcp.storage.hub import ASK, Hub, topic
from aiohttp_tiny_mcp.storage.sessions import Session, SessionAccess
from aiohttp_tiny_mcp.storage.sessions import new_session_id as new_id
from aiohttp_tiny_mcp.transport.sse import SSEResponse

if TYPE_CHECKING:
    from .registry import Registry

log = logging.getLogger("aiohttp_tiny_mcp")


def waiting_for(request: InputRequest, registry: Registry) -> float:
    """How long to wait for this reply. A model call waits longer than a person does."""
    if str(request.get("method")) == "sampling/createMessage":
        return registry.completion_timeout_seconds
    return registry.ask_timeout_seconds


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
        try:
            async with self.stack:
                yield
        finally:
            self.resolved.clear()

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
    def can_push_request(self) -> bool:
        """Whether any server request can go out on this request's stream.

        A question has its own answer in `can_ask`, because elicitation arrived two
        revisions after a server request did.
        """
        return self.adapter.can_push_request and self.can_complete

    @property
    def can_complete(self) -> bool:
        """Whether this client offers its model at all."""
        return self.call.client.capabilities.get("sampling") is not None

    async def completion(
        self,
        *,
        messages: Sequence[CompletionMessage | Mapping[str, Any]],
        max_tokens: int,
        key: str = COMPLETION_KEY,
        system_prompt: str | None = None,
        model_preferences: Mapping[str, Any] | None = None,
        include_context: Literal["none", "thisServer", "allServers"] | None = None,
        temperature: float | None = None,
        stop_sequences: Sequence[str] | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> CompletionResult:
        """Ask the client's model to complete the conversation, and return what it answered.

        The client chooses the model and may refuse. A refusal is a failure here rather
        than an empty result, because a completion that did not happen is not a
        completion. Use `key` to make more than one call in one handler.

        The request travels the way the revision allows. On the revision that resolves
        by restart, this raises `NeedInput` and the handler runs again with the answer.
        An answer already carried by the call is reused.
        """
        request = CompletionRequest(
            messages=messages,
            max_tokens=max_tokens,
            system_prompt=system_prompt,
            model_preferences=model_preferences,
            include_context=include_context,
            temperature=temperature,
            stop_sequences=stop_sequences,
            metadata=metadata,
        )
        return self._read_completion(await self.request_input(key, request.as_input()))

    def completed(self, key: str = COMPLETION_KEY) -> CompletionResult | None:
        """The completion this call already carries, or None where it carries none."""
        if key not in self.call.answers:
            return None
        return self._read_completion(self.call.answers[key])

    @staticmethod
    def _read_completion(reply: Mapping[str, Any]) -> CompletionResult:
        """Convert an invalid completion reply to a protocol failure."""
        try:
            return CompletionResult.from_reply(reply)
        except ValueError as error:
            raise Rejected(Failure(FailureKind.MALFORMED, str(error))) from None

    async def request_input(self, key: str, request: InputRequest) -> Mapping[str, Any]:
        """Put one server request to the client, through whatever channel there is.

        Returns the reply as it arrived, so a caller that is not a question reads its
        own shape instead of an elicitation answer. Where this revision cannot push the
        request, `NeedInput` hands it to the dispatcher, which resolves it by restart.
        """
        if self.answered(key):
            return self.call.answers.get(key) or {}
        needed = NEEDED_CAPABILITY.get(str(request.get("method")))
        if self.adapter.can_push_request and (
            needed is None or self.call.client.capabilities.get(needed) is not None
        ):
            return await self.push(key, request, timeout=waiting_for(request, self.registry))
        raise NeedInput({key: request})

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
        """Send a question on the active stream and wait for the answer."""
        reply = await self.push(key, request)
        return answer_of(reply) if reply else Answer(action=AnswerAction.CANCEL)

    async def push(
        self, key: str, request: InputRequest, *, timeout: float | None = None
    ) -> Mapping[str, Any]:
        """Send one server request on the active stream and wait through the hub.

        Returns the reply as it arrived, or an empty mapping where the wait ended
        without one. The reply may reach another node, so the subscription is made
        before the request goes out and a fast answer is not missed.
        """
        hub = self.registry.hub
        wire_id = new_id()
        where = topic(ASK, wire_id)
        replies = await hub.subscribe(where, wait=self.registry.hub_poll_seconds)
        await self.emit({"jsonrpc": "2.0", "id": wire_id, **request})
        loop = asyncio.get_running_loop()
        waiting = self.registry.ask_timeout_seconds if timeout is None else timeout
        deadline = loop.time() + waiting
        try:
            while not self.cancelled.is_set():
                left = deadline - loop.time()
                if left <= 0:
                    return {}
                for event in await replies.poll(timeout=min(left, replies.wait)):
                    reply = event.message.get("reply")
                    if isinstance(reply, Mapping):
                        return reply
            return {}
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
        text = serialized(payload).decode()
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
