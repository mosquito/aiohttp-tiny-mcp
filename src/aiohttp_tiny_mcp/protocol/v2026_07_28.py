"""2026-07-28: no handshake, per-request `_meta`, MRTR, mirrored headers."""

from __future__ import annotations

import base64
from collections.abc import Mapping
from functools import lru_cache
from types import MappingProxyType
from typing import Any, ClassVar, Literal

from pydantic import Field

from aiohttp_tiny_mcp.adapter import Adapter, RegistryProtocol
from aiohttp_tiny_mcp.core import (
    Call,
    ClientInfo,
    ClientProfile,
    Failure,
    FailureKind,
    InputRequest,
    NeedsInput,
    Operation,
    Preamble,
    Rejected,
    unwrap_answers,
)
from aiohttp_tiny_mcp.models import (
    CallToolParams,
    CompleteParams,
    GetPromptParams,
    Incoming,
    ListenParams,
    ListParams,
    Meta,
    Model,
    Params,
    ReadResourceParams,
    ReadResourceResult,
    ToolDef,
)
from aiohttp_tiny_mcp.schema import header_params
from aiohttp_tiny_mcp.specs import ToolSpec

# Modern subscriptions are independent long-lived requests, not MCP sessions.
METHODS: Mapping[str, Operation] = MappingProxyType(
    {
        "server/discover": Operation.DESCRIBE,
        "tools/list": Operation.LIST_TOOLS,
        "tools/call": Operation.CALL_TOOL,
        "resources/list": Operation.LIST_RESOURCES,
        "resources/templates/list": Operation.LIST_RESOURCE_TEMPLATES,
        "resources/read": Operation.READ_RESOURCE,
        "prompts/list": Operation.LIST_PROMPTS,
        "prompts/get": Operation.GET_PROMPT,
        "completion/complete": Operation.COMPLETE,
        "subscriptions/listen": Operation.LISTEN,
    }
)
METHOD_NAMES: Mapping[Operation, str] = MappingProxyType({op: name for name, op in METHODS.items()})

PARAMS_MODELS: Mapping[Operation, type[Params]] = MappingProxyType(
    {
        Operation.LIST_TOOLS: ListParams,
        Operation.CALL_TOOL: CallToolParams,
        Operation.LIST_RESOURCES: ListParams,
        Operation.LIST_RESOURCE_TEMPLATES: ListParams,
        Operation.READ_RESOURCE: ReadResourceParams,
        Operation.LIST_PROMPTS: ListParams,
        Operation.GET_PROMPT: GetPromptParams,
        Operation.COMPLETE: CompleteParams,
        Operation.LISTEN: ListenParams,
    }
)

NAME_HEADER_METHODS = frozenset({"tools/call", "resources/read", "prompts/get"})


@lru_cache(maxsize=1024)
def decode_header(value: str) -> str:
    """Undo the `=?base64?...?=` sentinel used for non-ASCII header values."""
    if value.startswith("=?base64?") and value.endswith("?="):
        return base64.b64decode(value[9:-2], validate=True).decode("utf-8")
    return value


def header_text(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int) and -(2**53) + 1 <= value <= 2**53 - 1:
        return str(value)
    if isinstance(value, str):
        return value
    raise ValueError("mirrored parameter must be a string, boolean, or safe integer")


def encode_header(value: Any) -> str:
    text = header_text(value)
    unsafe = text != text.strip(" \t") or any(
        ord(char) < 0x20 or ord(char) >= 0x7F for char in text
    )
    if unsafe:
        encoded = base64.b64encode(text.encode()).decode("ascii")
        return f"=?base64?{encoded}?="
    return text


MISSING = object()


def path_value(arguments: Mapping[str, Any], path: tuple[str, ...]) -> Any:
    value: Any = arguments
    for part in path:
        if not isinstance(value, Mapping) or part not in value:
            return MISSING
        value = value[part]
    return value


class DiscoverResult(Model):
    result_type: Literal["complete"] = "complete"
    supported_versions: list[str]
    capabilities: dict[str, Any]
    instructions: str | None = None
    ttl_ms: int | None = None
    cache_scope: Literal["private", "public"] | None = None
    meta: Meta | None = Field(None, alias="_meta")


class InputRequiredResult(Model):
    result_type: Literal["input_required"] = "input_required"
    input_requests: dict[str, Any]
    request_state: str | None = None
    meta: Meta | None = Field(None, alias="_meta")


class Adapter2026_07_28(Adapter):  # noqa: N801 -- revision date, greppable against the spec
    version: ClassVar[str] = "2026-07-28"
    can_ask: ClassVar[bool] = True

    FAILURE_MAP: ClassVar[Mapping[FailureKind, tuple[int, int]]] = MappingProxyType(
        {
            **Adapter.BASE_FAILURE_MAP,
            FailureKind.UNKNOWN_METHOD: (-32601, 404),
            FailureKind.UNKNOWN_TARGET: (-32601, 404),
            FailureKind.RESOURCE_NOT_FOUND: (-32602, 404),
            FailureKind.HEADER_MISMATCH: (-32020, 400),
            FailureKind.INPUT_UNSUPPORTED: (-32603, 200),
            FailureKind.MISSING_REQUIRED_CAPABILITY: (-32021, 400),
            FailureKind.UNSUPPORTED_VERSION: (-32022, 400),
        }
    )

    def __init__(self) -> None:
        self.supported_versions: tuple[str, ...] = (self.version,)

    def bind(self, versions: tuple[str, ...]) -> None:
        self.supported_versions = versions

    def check_http(
        self,
        pre: Preamble,
        headers: Mapping[str, str],
        registry: RegistryProtocol | None = None,
    ) -> None:
        is_notification = isinstance(pre.body, dict) and "id" not in pre.body
        if pre.meta_version is None:
            raise Rejected(
                Failure(FailureKind.MALFORMED, "missing protocol version in request _meta")
            )
        if pre.header_version is None:
            if is_notification:
                return
            raise Rejected(
                Failure(FailureKind.HEADER_MISMATCH, "missing MCP-Protocol-Version header")
            )
        if pre.header_version != pre.meta_version:
            raise Rejected(
                Failure(FailureKind.HEADER_MISMATCH, "MCP-Protocol-Version does not match _meta")
            )
        method = headers.get("Mcp-Method")
        if method is None:
            raise Rejected(Failure(FailureKind.HEADER_MISMATCH, "missing Mcp-Method header"))
        if method != pre.method:
            raise Rejected(
                Failure(
                    FailureKind.HEADER_MISMATCH,
                    f"Mcp-Method {method!r} != body method {pre.method!r}",
                )
            )
        if pre.method not in NAME_HEADER_METHODS:
            return
        name = headers.get("Mcp-Name")
        if name is None:
            raise Rejected(Failure(FailureKind.HEADER_MISMATCH, "missing Mcp-Name header"))
        params = pre.body.get("params") if isinstance(pre.body, dict) else None
        key = "uri" if pre.method == "resources/read" else "name"
        target = params.get(key) if isinstance(params, dict) else None
        try:
            decoded_name = decode_header(name)
        except (ValueError, UnicodeDecodeError):
            raise Rejected(
                Failure(FailureKind.HEADER_MISMATCH, "malformed Mcp-Name header")
            ) from None
        if decoded_name != target:
            raise Rejected(Failure(FailureKind.HEADER_MISMATCH, "Mcp-Name does not match body"))
        if pre.method == "tools/call" and registry is not None:
            spec = registry.tools.get(target or "")
            arguments = params.get("arguments", {}) if isinstance(params, dict) else {}
            if spec is not None and isinstance(arguments, Mapping):
                self.check_parameter_headers(spec, arguments, headers)

    def check_parameter_headers(
        self,
        spec: ToolSpec,
        arguments: Mapping[str, Any],
        headers: Mapping[str, str],
    ) -> None:
        for path, suffix in spec.header_params:
            header_name = f"Mcp-Param-{suffix}"
            actual = headers.get(header_name)
            expected_value = path_value(arguments, path)
            if expected_value is MISSING or expected_value is None:
                if actual is not None:
                    raise Rejected(
                        Failure(
                            FailureKind.HEADER_MISMATCH,
                            f"{header_name} has no matching body parameter",
                        )
                    )
                continue
            if actual is None:
                raise Rejected(
                    Failure(FailureKind.HEADER_MISMATCH, f"missing {header_name} header")
                )
            try:
                expected = header_text(expected_value)
                decoded = decode_header(actual)
            except (ValueError, UnicodeDecodeError):
                raise Rejected(
                    Failure(FailureKind.HEADER_MISMATCH, f"malformed {header_name} header")
                ) from None
            if decoded != expected:
                raise Rejected(
                    Failure(
                        FailureKind.HEADER_MISMATCH,
                        f"{header_name} does not match body parameter",
                    )
                )

    def params_model(self, operation: Operation) -> type[Params]:
        return PARAMS_MODELS.get(operation, Params)

    def check_message(self, operation: Operation, msg: Incoming) -> None:
        if operation is Operation.LISTEN and msg.is_notification:
            raise Rejected(Failure(FailureKind.MALFORMED, "subscriptions/listen requires an id"))

    def check_params(self, params: Params) -> None:
        """Context is required on every request; there are no handshake defaults."""
        if params.meta.protocol_version != self.version:
            raise Rejected(
                Failure(FailureKind.MALFORMED, "missing or incorrect protocol version in _meta")
            )
        if params.meta.client_capabilities is None:
            raise Rejected(
                Failure(FailureKind.MALFORMED, "missing client capabilities in request _meta")
            )

    def client_info_for(self, params: Params) -> ClientInfo:
        info = params.meta.client_info
        return ClientInfo(
            name=info.name if info is not None else None,
            version=info.version if info is not None else None,
            capabilities=params.meta.client_capabilities or {},
        )

    def answers_for(self, params: Params) -> Mapping[str, Any]:
        return unwrap_answers(params.input_responses)

    def operation_for(self, method: str) -> Operation | None:
        return METHODS.get(method)

    def method_for(self, operation: Operation) -> str | None:
        return METHOD_NAMES.get(operation)

    def encode_value(self, call: Call, registry: RegistryProtocol, result: Any) -> dict[str, Any]:
        meta = getattr(result, "meta", None) or Meta()
        updates: dict[str, Any] = {"meta": meta.model_copy(update={"server_info": registry.info})}
        if getattr(result, "result_type", "absent") is None:
            updates["result_type"] = "complete"
        if getattr(result, "ttl_ms", "absent") is None:
            ttl_ms, cache_scope = 0, "private"
            if isinstance(result, ReadResourceResult):
                spec = registry.match_resource(call.target or "")
                if spec is not None:
                    if spec[0].cache_ttl_ms is not None:
                        ttl_ms = spec[0].cache_ttl_ms
                    if spec[0].cache_scope is not None:
                        cache_scope = spec[0].cache_scope
            updates["ttl_ms"] = ttl_ms
            updates["cache_scope"] = cache_scope
        result = result.model_copy(update=updates)
        return {"jsonrpc": "2.0", "id": call.id, "result": result.wire()}

    def encode_input_required(
        self, call: Call, registry: RegistryProtocol, out: NeedsInput
    ) -> dict[str, Any]:
        if out.state is not None and not isinstance(out.state, str):
            raise TypeError("requestState must be sealed before wire encoding")
        result = InputRequiredResult(
            input_requests=dict(out.requests),
            request_state=out.state,
            meta=Meta(server_info=registry.info),
        )
        return {"jsonrpc": "2.0", "id": call.id, "result": result.wire()}

    def describe_server(self, registry: RegistryProtocol, call: Call) -> DiscoverResult:
        return DiscoverResult(
            supported_versions=list(self.supported_versions),
            capabilities=self.capabilities(registry),
            instructions=registry.instructions,
        )

    def describe_tool(self, spec: ToolSpec) -> ToolDef | None:
        if spec.min_revision is not None and spec.min_revision > self.version:
            return None
        return spec.definition(spec.input_schema, spec.output_schema)

    def client_headers(
        self,
        method: str,
        name: str | None = None,
        params: Mapping[str, Any] | None = None,
        tool: ToolDef | None = None,
    ) -> Mapping[str, str]:
        headers = {"MCP-Protocol-Version": self.version, "Mcp-Method": method}
        if name is not None and method in NAME_HEADER_METHODS:
            headers["Mcp-Name"] = encode_header(name)
        if method == "tools/call" and params is not None and tool is not None:
            arguments = params.get("arguments", {})
            if isinstance(arguments, Mapping):
                for path, suffix in header_params(tool.input_schema):
                    value = path_value(arguments, path)
                    if value is not MISSING and value is not None:
                        headers[f"Mcp-Param-{suffix}"] = encode_header(value)
        return headers

    def client_handshake_params(self, client: ClientProfile) -> Params:
        return Params()

    def client_decorate_params(self, params: Params, client: ClientProfile) -> Params:
        # No handshake or session: send client context on every request.
        params.meta.protocol_version = self.version
        params.meta.client_info = client.info
        params.meta.client_capabilities = dict(client.capabilities)
        params.meta.log_level = client.log_level
        return params

    def client_input_requests(
        self, result: Mapping[str, Any]
    ) -> tuple[Mapping[str, InputRequest], Any] | None:
        if result.get("resultType") != "input_required":
            return None
        return result.get("inputRequests") or {}, result.get("requestState")
