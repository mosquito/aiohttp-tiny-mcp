"""Maps Operation to registry lookups and produces an Outcome. Knows nothing
about HTTP, headers or protocol versions.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from .auth import Principal
from .core import (
    LOG_LEVELS,
    Failure,
    FailureKind,
    NeedInput,
    NeedsInput,
    Operation,
    Outcome,
    Rejected,
    Value,
)
from .exchange import Exchange
from .models import (
    CallToolResult,
    CompleteResult,
    Completion,
    EmptyResult,
    GetPromptResult,
    ListParams,
    ListPromptsResult,
    ListResourcesResult,
    ListResourceTemplatesResult,
    ListToolsResult,
    PromptDef,
    PromptMessage,
    ReadResourceResult,
    ResourceDef,
    ResourceTemplateDef,
    ResultModel,
    SetLevelParams,
    SubscribeParams,
    TextContent,
    ToolDef,
)
from .registry import Registry
from .request_state import RequestStates, protect_request_state, restore_request_state
from .specs import ToolSpec
from .subscriptions import listen, subscribe, unsubscribe


def prompt_result(value: Any) -> GetPromptResult:
    if isinstance(value, GetPromptResult):
        return value
    if isinstance(value, str):
        return GetPromptResult(
            messages=[PromptMessage(role="user", content=TextContent(text=value))]
        )
    return GetPromptResult(messages=list(value))


def refusal(spec: ToolSpec, principal: Principal | None) -> str | None:
    """Why this caller may not call this tool, or `None` where it may.

    A tool that names scopes is unreachable without a verified caller, on any
    transport. Over stdio there is no token to carry one, and silently
    allowing the call there would make the scope a comment.
    """
    if not spec.scopes:
        return None
    if principal is None:
        return (
            f"{spec.name} requires the scopes {', '.join(sorted(spec.scopes))}, and this "
            f"request carried no verified caller"
        )
    missing = principal.holds(spec.scopes)
    if missing:
        return f"{spec.name} requires the scope {', '.join(sorted(missing))}"
    return None


PUSH_ASK_ROUNDS = 8

Entry = TypeVar("Entry")


class Dispatcher:
    def __init__(self, registry: Registry) -> None:
        self.registry = registry

    @property
    def request_state(self) -> RequestStates:
        return self.registry.request_state

    async def run(self, ex: Exchange) -> Outcome:
        resumed: str | None = None
        if ex.adapter.carries_state:
            resumed = ex.call.state if isinstance(ex.call.state, str) else None
            failure = await restore_request_state(ex.call, self.request_state)
            if failure is not None:
                return failure
        operation = ex.call.operation
        try:
            outcome = await self.dispatch(operation, ex)
        except NeedInput as e:
            outcome = await self.resolve_need_input(ex, e)
        except Rejected as e:
            outcome = e.failure
        except Exception as e:  # noqa: BLE001 -- last resort, see the per-operation handlers
            outcome = Failure(FailureKind.INTERNAL, f"{type(e).__name__}: {e}")

        if isinstance(outcome, NeedsInput):
            outcome = await protect_request_state(ex.call, outcome, self.request_state)
        if resumed is not None:
            await self.request_state.drop(resumed)
        return outcome

    async def resolve_need_input(self, ex: Exchange, e: NeedInput) -> Outcome:
        if ex.can_ask and ex.adapter.can_push_ask and not ex.adapter.can_ask:
            return await self.ask_on_the_stream(ex, e)
        if ex.can_ask:
            return NeedsInput(requests=e.requests, state=e.state)
        if ex.adapter.can_ask:
            return Failure(
                FailureKind.MISSING_REQUIRED_CAPABILITY,
                "client did not declare the elicitation capability",
                data={"requiredCapabilities": {"elicitation": {}}},
            )
        return Failure(FailureKind.INPUT_UNSUPPORTED, f"{ex.adapter.version} cannot ask the user")

    async def ask_on_the_stream(self, ex: Exchange, first: NeedInput) -> Outcome:
        """Put each question to the client, then run the handler again.

        The answers are written where the handler reads them, so one handler
        serves both mechanics: a revision that carries questions in its result
        restarts the call, and this one restarts the handler in place.
        """
        asked = first
        for _ in range(PUSH_ASK_ROUNDS):
            answers = dict(ex.call.answers)
            actions = dict(ex.call.actions)
            for key, request in asked.requests.items():
                answer = await ex.push_ask(key, request)
                answers[key] = dict(answer.content)
                actions[key] = answer.action
            ex.call.answers = answers
            ex.call.actions = actions
            ex.call.state = asked.state
            try:
                return await self.dispatch(ex.call.operation, ex)
            except NeedInput as again:
                asked = again
        return Failure(
            FailureKind.INTERNAL,
            f"{ex.call.target} still asking after {PUSH_ASK_ROUNDS} rounds",
        )

    async def dispatch(self, operation: Operation, ex: Exchange) -> Outcome:
        """Dispatch explicitly so type checking catches missing or renamed handlers."""
        match operation:
            case Operation.DESCRIBE:
                return await self.describe(ex)
            case Operation.HANDSHAKE_COMPLETE:
                return await self.handshake_complete(ex)
            case Operation.PING:
                return await self.ping(ex)
            case Operation.LIST_TOOLS:
                return await self.list_tools(ex)
            case Operation.CALL_TOOL:
                return await self.call_tool(ex)
            case Operation.LIST_RESOURCES:
                return await self.list_resources(ex)
            case Operation.LIST_RESOURCE_TEMPLATES:
                return await self.list_resource_templates(ex)
            case Operation.READ_RESOURCE:
                return await self.read_resource(ex)
            case Operation.LIST_PROMPTS:
                return await self.list_prompts(ex)
            case Operation.GET_PROMPT:
                return await self.get_prompt(ex)
            case Operation.COMPLETE:
                return await self.complete(ex)
            case Operation.LISTEN:
                return Value(result=await listen(ex))
            case Operation.SET_LOG_LEVEL:
                return await self.set_log_level(ex)
            case Operation.SUBSCRIBE:
                return await self.subscribe(ex, wanted=True)
            case Operation.UNSUBSCRIBE:
                return await self.subscribe(ex, wanted=False)
            case Operation.EXTENSION:
                return await self.call_extension(ex)

    async def call_extension(self, ex: Exchange) -> Outcome:
        method = ex.call.target or ""
        for extension in self.registry.extensions.values():
            bound = extension.methods.get(method)
            if (
                bound is None
                or not ex.adapter.supports_extensions
                or extension.min_revision > ex.adapter.version
            ):
                continue
            try:
                value = await bound.call(dict(ex.call.arguments), ex)
            except ValidationError as e:
                return Failure(FailureKind.INVALID_PARAMS, str(e))
            if isinstance(value, ResultModel):
                return Value(result=value)
            if isinstance(value, BaseModel):
                value = value.model_dump(mode="json", by_alias=True)
            if not isinstance(value, Mapping):
                raise TypeError(f"{method}: extension handler must return a result object")
            return Value(result=ResultModel.model_validate(dict(value)))
        return Failure(FailureKind.UNKNOWN_METHOD, f"unknown method: {method}")

    async def set_log_level(self, ex: Exchange) -> Outcome:
        """Persist the log level in the session for subsequent requests on any node."""
        assert isinstance(ex.call.params, SetLevelParams)
        level = ex.call.params.level
        if level not in LOG_LEVELS:
            return Failure(FailureKind.INVALID_PARAMS, f"unknown severity {level!r}")
        if ex.session is not None:
            await ex.session.set_log_level(level)
        elif ex.keep_log_level is not None:
            ex.keep_log_level(level)
        else:
            return Failure(
                FailureKind.INVALID_PARAMS,
                "logging/setLevel needs the session the handshake issued",
            )
        return Value(result=EmptyResult())

    async def subscribe(self, ex: Exchange, *, wanted: bool) -> Outcome:
        """Persist resource subscriptions for the node serving the notification stream."""
        assert isinstance(ex.call.params, SubscribeParams)
        uri = ex.call.params.uri
        if ex.session is None:
            return Failure(
                FailureKind.INVALID_PARAMS,
                "resources/subscribe needs the session the handshake issued",
            )
        if wanted:
            found = self.registry.match_resource(uri)
            definition = ex.adapter.describe_resource(found[0]) if found is not None else None
            if definition is None or (
                isinstance(definition, ResourceDef) and definition.uri != uri
            ):
                return Failure(FailureKind.RESOURCE_NOT_FOUND, f"no resource matches {uri}")
        await (subscribe if wanted else unsubscribe)(ex.session, uri)
        return Value(result=EmptyResult())

    async def describe(self, ex: Exchange) -> Outcome:
        return Value(result=ex.adapter.describe_server(self.registry, ex.call))

    async def handshake_complete(self, ex: Exchange) -> Outcome:
        return Value(result=EmptyResult())

    async def ping(self, ex: Exchange) -> Outcome:
        return Value(result=EmptyResult())

    def paged(
        self, ex: Exchange, entries: list[Entry], key: Callable[[Entry], str]
    ) -> tuple[list[Entry], str | None] | Failure:
        """One page of `entries`, sorted by `key`, and the cursor for the next.

        The cursor is the key of the last entry shown. One that names no entry is
        -32602: a silent first page would send a paging client round in a circle.
        """
        cursor = ex.call.params.cursor if isinstance(ex.call.params, ListParams) else None
        if cursor is not None:
            if all(key(entry) != cursor for entry in entries):
                return Failure(FailureKind.INVALID_PARAMS, f"unknown cursor: {cursor}")
            entries = [entry for entry in entries if key(entry) > cursor]
        size = self.registry.page_size
        if size is None or len(entries) <= size:
            return entries, None
        shown = entries[:size]
        return shown, key(shown[-1])

    async def list_tools(self, ex: Exchange) -> Outcome:
        tools: list[ToolDef] = [
            d
            for spec in self.registry.tools.values()
            if (d := ex.adapter.describe_tool(spec)) is not None
        ]
        tools.sort(key=lambda item: item.name)
        found = self.paged(ex, tools, lambda item: item.name)
        if isinstance(found, Failure):
            return found
        shown, next_cursor = found
        return Value(result=ListToolsResult(tools=shown, next_cursor=next_cursor))

    async def call_tool(self, ex: Exchange) -> Outcome:
        spec = self.registry.tools.get(ex.call.target or "")
        if spec is None or ex.adapter.describe_tool(spec) is None:
            return Failure(FailureKind.UNKNOWN_TARGET, f"unknown tool: {ex.call.target}")
        if (refused := refusal(spec, ex.principal)) is not None:
            return Value(result=CallToolResult.failure(refused))
        try:
            value = await spec.bound.call(dict(ex.call.arguments), ex)
        except ValidationError as e:
            return Value(result=CallToolResult.failure(f"invalid arguments:\n{e}"))
        except NeedInput:
            raise
        except Exception as e:  # noqa: BLE001 -- tool faults are results, not RPC errors
            return Value(result=CallToolResult.failure(f"{type(e).__name__}: {e}"))
        return Value(result=CallToolResult.of(value))

    async def list_resources(self, ex: Exchange) -> Outcome:
        defs: list[ResourceDef] = [
            d
            for spec in self.registry.resources_fixed.values()
            if isinstance(d := ex.adapter.describe_resource(spec), ResourceDef)
        ]
        defs.sort(key=lambda item: item.uri)
        found = self.paged(ex, defs, lambda item: item.uri)
        if isinstance(found, Failure):
            return found
        shown, next_cursor = found
        return Value(result=ListResourcesResult(resources=shown, next_cursor=next_cursor))

    async def list_resource_templates(self, ex: Exchange) -> Outcome:
        defs: list[ResourceTemplateDef] = [
            d
            for spec in self.registry.resources_templated
            if isinstance(d := ex.adapter.describe_resource(spec), ResourceTemplateDef)
        ]
        defs.sort(key=lambda item: item.uri_template)
        found = self.paged(ex, defs, lambda item: item.uri_template)
        if isinstance(found, Failure):
            return found
        shown, next_cursor = found
        return Value(
            result=ListResourceTemplatesResult(resource_templates=shown, next_cursor=next_cursor)
        )

    async def read_resource(self, ex: Exchange) -> Outcome:
        uri = ex.call.target or ""
        found = self.registry.match_resource(uri)
        if found is None:
            return Failure(FailureKind.RESOURCE_NOT_FOUND, f"resource not found: {uri}")
        spec, variables = found
        definition = ex.adapter.describe_resource(spec)
        if definition is None or (isinstance(definition, ResourceDef) and definition.uri != uri):
            return Failure(FailureKind.RESOURCE_NOT_FOUND, f"resource not found: {uri}")
        try:
            value = await spec.bound.call(variables, ex)
        except ValidationError as e:
            return Failure(FailureKind.INVALID_PARAMS, str(e))
        return Value(result=ReadResourceResult(contents=spec.contents(uri, value)))

    async def list_prompts(self, ex: Exchange) -> Outcome:
        prompts: list[PromptDef] = [
            d
            for spec in self.registry.prompts.values()
            if (d := ex.adapter.describe_prompt(spec)) is not None
        ]
        prompts.sort(key=lambda item: item.name)
        found = self.paged(ex, prompts, lambda item: item.name)
        if isinstance(found, Failure):
            return found
        shown, next_cursor = found
        return Value(result=ListPromptsResult(prompts=shown, next_cursor=next_cursor))

    async def get_prompt(self, ex: Exchange) -> Outcome:
        spec = self.registry.prompts.get(ex.call.target or "")
        if spec is None or ex.adapter.describe_prompt(spec) is None:
            return Failure(FailureKind.UNKNOWN_TARGET, f"unknown prompt: {ex.call.target}")
        try:
            value = await spec.bound.call(dict(ex.call.arguments), ex)
        except ValidationError as e:
            return Failure(FailureKind.INVALID_PARAMS, str(e))
        return Value(result=prompt_result(value))

    async def complete(self, ex: Exchange) -> Outcome:
        completer = self.registry.completer
        if completer is None:
            return Value(result=CompleteResult(completion=Completion(values=[])))
        value = await completer.call(dict(ex.call.arguments), ex)
        if isinstance(value, Completion):
            return Value(result=CompleteResult(completion=value))
        values = list(value)[:100]
        return Value(result=CompleteResult(completion=Completion(values=values, total=len(values))))
