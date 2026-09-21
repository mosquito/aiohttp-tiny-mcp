"""Pre-elicitation adapter: supports batching and exchanges answers through tool arguments."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, ClassVar

from aiohttp_tiny_mcp.protocol.core import (
    AnswerAction,
    Call,
    ClientProfile,
    InputRequest,
    Operation,
    answer_actions,
    unwrap_answers,
)
from aiohttp_tiny_mcp.protocol.models import (
    CallToolParams,
    CallToolResult,
    Incoming,
    Meta,
    Params,
    ResourceLink,
    TextContent,
    ToolDef,
)
from aiohttp_tiny_mcp.server.specs import ToolSpec

from .v2025_06_18 import Adapter2025_06_18

ANSWERS_KEY = "mcpAnswers"

STATE_KEY = "mcpState"

ASKED_KEY = "dev.aiohttp-tiny-mcp/inputRequired"

ANSWERS_DESCRIPTION = (
    "Answers to questions this tool asked. Send it back exactly as the tool's "
    'previous result gave it, together with "' + STATE_KEY + '". Omit both on '
    "the first call."
)

STATE_DESCRIPTION = (
    "Opaque value from this tool's previous result. Send it back unchanged "
    'together with "' + ANSWERS_KEY + '". Omit it on the first call.'
)


class Adapter2025_03_26(Adapter2025_06_18):  # noqa: N801 -- revision date, greppable against the spec
    version: ClassVar[str] = "2025-03-26"
    allows_batch: ClassVar[bool] = True
    can_push_ask: ClassVar[bool] = False
    asks_in_arguments: ClassVar[bool] = True

    def describe_resource(self, spec: Any) -> Any:
        definition = super().describe_resource(spec)
        return None if definition is None else definition.model_copy(update={"title": None})

    def describe_prompt(self, spec: Any) -> Any:
        definition = super().describe_prompt(spec)
        return None if definition is None else definition.model_copy(update={"title": None})

    def encode_value(self, call: Call, registry: Any, result: Any) -> dict[str, Any]:
        if isinstance(result, CallToolResult):
            update: dict[str, Any] = {}
            if result.structured_content is not None:
                update["structured_content"] = None
            kept = [block for block in result.content if not isinstance(block, ResourceLink)]
            if len(kept) != len(result.content):
                update["content"] = kept
            if update:
                result = result.model_copy(update=update)
        return super().encode_value(call, registry, result)

    def client_headers(
        self,
        method: str,
        name: str | None = None,
        params: Mapping[str, Any] | None = None,
        tool: Any = None,
    ) -> Mapping[str, str]:
        return {}

    def may_ask(self, spec: ToolSpec) -> bool:
        """Only handlers receiving Exchange need the extra answer/state arguments."""
        return any(kind.__name__ == "Exchange" for _, kind in spec.bound.plan)

    def describe_tool(self, spec: ToolSpec) -> ToolDef | None:
        definition = super().describe_tool(spec)
        if definition is None:
            return None
        definition = definition.model_copy(update={"output_schema": None, "title": None})
        if not self.may_ask(spec):
            return definition
        schema = dict(definition.input_schema)
        properties = dict(schema.get("properties") or {})
        properties[ANSWERS_KEY] = {"type": "object", "description": ANSWERS_DESCRIPTION}
        properties[STATE_KEY] = {"type": "string", "description": STATE_DESCRIPTION}
        schema["properties"] = properties
        return definition.model_copy(update={"input_schema": schema})

    def build_call(self, operation: Operation, msg: Incoming, params: Params) -> Call:
        call = super().build_call(operation, msg, params)
        if operation is not Operation.CALL_TOOL:
            return call
        arguments = dict(call.arguments)
        answers = arguments.pop(ANSWERS_KEY, None)
        state = arguments.pop(STATE_KEY, None)
        call.arguments = arguments
        if isinstance(answers, Mapping):
            call.answers = unwrap_answers(answers)
            call.actions = answer_actions(answers)
        if isinstance(state, str):
            call.state = state
        return call

    def encode_input_required(self, call: Call, registry: Any, out: Any) -> Mapping[str, Any]:
        """Return retry instructions as a successful, unfinished tool result."""
        instructions = {
            ANSWERS_KEY: {
                key: {"action": AnswerAction.ACCEPT.value, "content": {}} for key in out.requests
            },
            STATE_KEY: out.state,
        }
        questions = "\n".join(
            f"- {key}: {(request.get('params') or {}).get('message', '')}"
            for key, request in out.requests.items()
        )
        text = (
            "This tool needs answers before it can finish. Ask the user, then "
            f"call it again with the same arguments plus:\n\n"
            f"{json.dumps(instructions, ensure_ascii=False, indent=2)}\n\n"
            f'Replace each "content" with what the user answered, or set '
            f'"action" to "decline" or "cancel". Questions:\n{questions}'
        )
        result = CallToolResult(
            content=[TextContent(text=text)],
            is_error=False,
            meta=Meta.model_validate(
                {ASKED_KEY: {"inputRequests": dict(out.requests), "requestState": out.state}}
            ),
        )
        return self.encode_value(call, registry, result)

    def client_input_requests(
        self, result: Mapping[str, Any]
    ) -> tuple[Mapping[str, InputRequest], Any] | None:
        meta = result.get("_meta")
        asked = meta.get(ASKED_KEY) if isinstance(meta, Mapping) else None
        if not isinstance(asked, Mapping):
            return None
        return asked.get("inputRequests") or {}, asked.get("requestState")

    def client_decorate_params(self, params: Params, client: ClientProfile) -> Params:
        params = super().client_decorate_params(params, client)
        if not isinstance(params, CallToolParams):
            return params
        if params.input_responses is not None:
            params.arguments[ANSWERS_KEY] = dict(params.input_responses)
            params.input_responses = None
        if params.request_state is not None:
            params.arguments[STATE_KEY] = params.request_state
            params.request_state = None
        return params
