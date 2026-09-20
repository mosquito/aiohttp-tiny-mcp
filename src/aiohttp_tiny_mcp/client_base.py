"""Shared client request building, result parsing, and elicitation handling."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from typing import Any

from .adapter import Adapter
from .core import ClientProfile, InputRequest, Operation
from .models import (
    CallToolParams,
    CallToolResult,
    CompleteArgument,
    CompleteParams,
    CompleteRef,
    CompleteResult,
    GetPromptParams,
    GetPromptResult,
    Implementation,
    ListenNotifications,
    ListenParams,
    ListParams,
    ListPromptsResult,
    ListResourcesResult,
    ListToolsResult,
    MethodFilter,
    Params,
    PromptDef,
    ReadResourceParams,
    ReadResourceResult,
    ResourceDef,
    SetLevelParams,
    SubscribeParams,
    ToolDef,
)

log = logging.getLogger(__name__)

Elicitor = Callable[[InputRequest], Awaitable[Mapping[str, Any]]]

ASK_ROUNDS = 8

ACKNOWLEDGED = "notifications/subscriptions/acknowledged"


class ClientError(Exception):
    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.data = data


class BaseClient(ABC):
    def __init__(
        self,
        adapter: Adapter,
        *,
        client_info: Implementation | None = None,
        on_ask: Elicitor | None = None,
        on_notification: Callable[[Mapping[str, Any]], Any] | None = None,
        log_level: str | None = None,
    ) -> None:
        self.adapter = adapter
        self.client_info = client_info or Implementation(
            name="aiohttp-tiny-mcp-client", version="0.1.0"
        )
        self.log_level = log_level
        self.on_ask = on_ask
        self.on_notification = on_notification
        self.next_id_counter = 0
        self.tool_definitions: dict[str, ToolDef] = {}
        self.accepted: Mapping[str, Any] = {}

    @property
    def capabilities(self) -> Mapping[str, Any]:
        """MRTR can be answered manually; pushed questions require an on_ask callback."""
        if self.on_ask is not None or self.adapter.can_ask:
            return {"elicitation": {}}
        return {}

    @property
    def profile(self) -> ClientProfile:
        """Client identity, capabilities, and desired log level for the adapter."""
        return ClientProfile(
            info=self.client_info, capabilities=self.capabilities, log_level=self.log_level
        )

    def next_id(self) -> int:
        self.next_id_counter += 1
        return self.next_id_counter

    @abstractmethod
    def exchange(
        self, envelope: dict[str, Any], *, method: str, name: str | None
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield messages as they arrive; waiting for EOF would deadlock pushed questions."""

    @abstractmethod
    async def send_notification(self, envelope: dict[str, Any], *, method: str) -> None:
        """Send a notification envelope (no `id`); no reply is expected."""

    @abstractmethod
    async def reply(self, envelope: dict[str, Any]) -> None:
        """Answer a request the server made. Carries no reply of its own."""

    def stream_notifications(
        self, *, last_event_id: str | None = None
    ) -> AsyncIterator[dict[str, Any]]:
        """Open a separate legacy notification channel, if the transport supports one."""
        raise NotImplementedError(
            f"{type(self).__name__} has no separate stream to read notifications on"
        )

    def answers(self, frame: Mapping[str, Any], request_id: Any) -> bool:
        """Match the request id, accepting null for errors raised before the server read it."""
        if "result" not in frame and "error" not in frame:
            return False
        return frame.get("id") == request_id or ("error" in frame and frame.get("id") is None)

    async def incoming(self, frame: Mapping[str, Any]) -> None:
        """Handle one message that is not the reply being waited for."""
        if frame.get("id") is None:
            if self.on_notification is not None:
                await_me = self.on_notification(frame)
                if isinstance(await_me, Awaitable):
                    await await_me
            return
        if self.on_ask is None or "method" not in frame:
            await self.reply(
                {
                    "jsonrpc": "2.0",
                    "id": frame["id"],
                    "error": {"code": -32601, "message": f"unsupported: {frame.get('method')}"},
                }
            )
            return
        answer = await self.on_ask({"method": frame["method"], "params": frame.get("params") or {}})
        await self.reply({"jsonrpc": "2.0", "id": frame["id"], "result": dict(answer)})

    async def request(
        self, operation: Operation, params: Params, *, name: str | None = None
    ) -> dict[str, Any]:
        method = self.adapter.method_for(operation)
        if method is None:
            raise ValueError(f"{self.adapter.version} does not expose {operation}")
        return await self.request_method(method, params, name=name)

    async def request_method(
        self,
        method: str,
        params: Params | Mapping[str, Any] | None = None,
        *,
        name: str | None = None,
    ) -> dict[str, Any]:
        """Call a named method, including extensions, with protocol metadata and headers.

        Check server discovery for the extension before using its methods.
        Results are returned as wire dictionaries; RPC errors raise ClientError.
        """
        if not isinstance(params, Params):
            params = Params.model_validate(dict(params or {}))
        params = self.adapter.client_decorate_params(params, self.profile)
        envelope = {
            "jsonrpc": "2.0",
            "id": self.next_id(),
            "method": method,
            "params": params.wire(),
        }
        body = None
        async for frame in self.exchange(envelope, method=method, name=name):
            if self.answers(frame, envelope["id"]):
                body = frame
                break
            await self.incoming(frame)
        if body is None:
            raise ClientError(-32000, f"{method} ended without a reply")
        if "error" in body:
            error = body["error"]
            raise ClientError(error["code"], error["message"], error.get("data"))
        return body["result"]

    async def notify(self, method: str, params: Params) -> None:
        params = self.adapter.client_decorate_params(params, self.profile)
        envelope = {"jsonrpc": "2.0", "method": method, "params": params.wire()}
        await self.send_notification(envelope, method=method)

    async def initialize(self) -> dict[str, Any]:
        params = self.adapter.client_handshake_params(self.profile)
        result = await self.request(Operation.DESCRIBE, params)
        complete_method = self.adapter.method_for(Operation.HANDSHAKE_COMPLETE)
        if complete_method is not None:
            await self.notify(complete_method, Params())
        if self.log_level is not None:
            await self.set_log_level(self.log_level)
        return result

    async def set_log_level(self, level: str) -> None:
        """Request this severity and above, via session state or per-request metadata."""
        if self.adapter.method_for(Operation.SET_LOG_LEVEL) is not None:
            await self.request(Operation.SET_LOG_LEVEL, SetLevelParams(level=level))
        self.log_level = level

    async def pages(self, operation: Operation) -> AsyncIterator[dict[str, Any]]:
        """Every page of a listing, following `nextCursor` to the end."""
        cursor: str | None = None
        while True:
            result = await self.request(operation, ListParams(cursor=cursor))
            yield result
            cursor = result.get("nextCursor")
            if not cursor:
                return

    async def list_tools(self) -> list[ToolDef]:
        tools: list[ToolDef] = []
        async for result in self.pages(Operation.LIST_TOOLS):
            tools += ListToolsResult.model_validate(result).tools
        self.tool_definitions = {tool.name: tool for tool in tools}
        return tools

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        input_responses: dict[str, Any] | None = None,
        request_state: Any = None,
        tool_definition: ToolDef | None = None,
    ) -> CallToolResult | dict[str, Any]:
        if tool_definition is not None:
            self.tool_definitions[name] = tool_definition
        answers = dict(input_responses or {})
        state = request_state
        for _ in range(ASK_ROUNDS):
            params = CallToolParams(
                name=name,
                arguments=arguments,
                input_responses=answers or None,
                request_state=state,
            )
            result = await self.request(Operation.CALL_TOOL, params, name=name)
            asked = self.adapter.client_input_requests(result)
            if asked is None:
                return CallToolResult.model_validate(result)
            if self.on_ask is None:
                # Without a callback, return questions for the caller to answer manually.
                return result
            requests, state = asked
            # Resend earlier answers because the handler restarts on each round.
            for key, request in requests.items():
                answers[key] = dict(await self.on_ask(request))
        raise ClientError(-32000, f"{name} still asking after {ASK_ROUNDS} rounds")

    async def listen(
        self,
        *,
        resources: Sequence[str] = (),
        tools_changed: bool = False,
        prompts_changed: bool = False,
        resources_changed: bool = False,
        methods: Sequence[str | MethodFilter | Mapping[str, Any]] = (),
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield changes until cancelled, via a request stream or legacy resource subscriptions.

        `methods` names the extension broadcasts to relay: a method name for
        every event of it, or `MethodFilter(method, topics)` (a mapping of
        the same shape will do) for the topics of a multicast. A legacy
        stream carries every broadcast the server declares, so there it is
        not sent.
        """
        wanted = ListenNotifications(
            tools_list_changed=tools_changed,
            prompts_list_changed=prompts_changed,
            resources_list_changed=resources_changed,
            resource_subscriptions=list(resources),
            methods=[
                entry if isinstance(entry, str | MethodFilter) else MethodFilter(**entry)
                for entry in methods
            ],
        )
        method = self.adapter.method_for(Operation.LISTEN)
        if method is not None:
            async for frame in self.listen_in_one_request(method, wanted):
                yield frame
            return
        for uri in resources:
            await self.request(Operation.SUBSCRIBE, SubscribeParams(uri=uri), name=uri)
        # Legacy list changes are enabled by the handshake capabilities.
        async for frame in self.stream_notifications():
            if "method" in frame and frame.get("id") is None:
                yield frame

    async def listen_in_one_request(
        self, method: str, wanted: ListenNotifications
    ) -> AsyncIterator[dict[str, Any]]:
        params = self.adapter.client_decorate_params(
            ListenParams(notifications=wanted), self.profile
        )
        envelope = {
            "jsonrpc": "2.0",
            "id": self.next_id(),
            "method": method,
            "params": params.wire(),
        }
        async for frame in self.exchange(envelope, method=method, name=None):
            if frame.get("id") == envelope["id"]:
                return  # the request answered, so the subscription is over
            if frame.get("method") == ACKNOWLEDGED:
                # Keep acknowledgment details separate from change notifications.
                self.accepted = (frame.get("params") or {}).get("notifications") or {}
                continue
            if "method" in frame:
                yield frame

    async def list_resources(self) -> list[ResourceDef]:
        found: list[ResourceDef] = []
        async for result in self.pages(Operation.LIST_RESOURCES):
            found += ListResourcesResult.model_validate(result).resources
        return found

    async def read_resource(self, uri: str) -> ReadResourceResult:
        result = await self.request(Operation.READ_RESOURCE, ReadResourceParams(uri=uri), name=uri)
        return ReadResourceResult.model_validate(result)

    async def list_prompts(self) -> list[PromptDef]:
        found: list[PromptDef] = []
        async for result in self.pages(Operation.LIST_PROMPTS):
            found += ListPromptsResult.model_validate(result).prompts
        return found

    async def get_prompt(self, name: str, arguments: dict[str, str]) -> GetPromptResult:
        result = await self.request(
            Operation.GET_PROMPT, GetPromptParams(name=name, arguments=arguments), name=name
        )
        return GetPromptResult.model_validate(result)

    async def complete(self, ref: dict[str, Any], argument: dict[str, str]) -> CompleteResult:
        params = CompleteParams(ref=CompleteRef(**ref), argument=CompleteArgument(**argument))
        result = await self.request(Operation.COMPLETE, params)
        return CompleteResult.model_validate(result)
