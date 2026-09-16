"""Legacy handshake adapter, also used as the base for earlier revisions."""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Any, ClassVar

from aiohttp_tiny_mcp.adapter import Adapter, RegistryProtocol
from aiohttp_tiny_mcp.core import (
    Call,
    ClientInfo,
    ClientProfile,
    Operation,
)
from aiohttp_tiny_mcp.models import (
    CallToolParams,
    CallToolResult,
    CompleteParams,
    GetPromptParams,
    Implementation,
    InitializeParams,
    ListParams,
    Model,
    Params,
    ReadResourceParams,
    SetLevelParams,
    SubscribeParams,
    ToolDef,
)
from aiohttp_tiny_mcp.schema import simplify_legacy_schema
from aiohttp_tiny_mcp.specs import ToolSpec

METHODS: Mapping[str, Operation] = MappingProxyType(
    {
        "initialize": Operation.DESCRIBE,
        "notifications/initialized": Operation.HANDSHAKE_COMPLETE,
        "ping": Operation.PING,
        "tools/list": Operation.LIST_TOOLS,
        "tools/call": Operation.CALL_TOOL,
        "resources/list": Operation.LIST_RESOURCES,
        "resources/templates/list": Operation.LIST_RESOURCE_TEMPLATES,
        "resources/read": Operation.READ_RESOURCE,
        "resources/subscribe": Operation.SUBSCRIBE,
        "resources/unsubscribe": Operation.UNSUBSCRIBE,
        "prompts/list": Operation.LIST_PROMPTS,
        "prompts/get": Operation.GET_PROMPT,
        "completion/complete": Operation.COMPLETE,
        "logging/setLevel": Operation.SET_LOG_LEVEL,
    }
)
METHOD_NAMES: Mapping[Operation, str] = MappingProxyType({op: name for name, op in METHODS.items()})

PARAMS_MODELS: Mapping[Operation, type[Params]] = MappingProxyType(
    {
        Operation.DESCRIBE: InitializeParams,
        Operation.LIST_TOOLS: ListParams,
        Operation.CALL_TOOL: CallToolParams,
        Operation.LIST_RESOURCES: ListParams,
        Operation.LIST_RESOURCE_TEMPLATES: ListParams,
        Operation.READ_RESOURCE: ReadResourceParams,
        Operation.SUBSCRIBE: SubscribeParams,
        Operation.UNSUBSCRIBE: SubscribeParams,
        Operation.LIST_PROMPTS: ListParams,
        Operation.GET_PROMPT: GetPromptParams,
        Operation.COMPLETE: CompleteParams,
        Operation.SET_LOG_LEVEL: SetLevelParams,
    }
)

#: initialize echoes any supported legacy revision, regardless of the selected adapter.
LEGACY_VERSIONS = frozenset({"2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"})


class InitializeResult(Model):
    protocol_version: str
    capabilities: dict[str, Any]
    server_info: Implementation
    instructions: str | None = None


def strip_header_annotations(schema: dict[str, Any]) -> dict[str, Any]:
    """Legacy clients cannot mirror `x-mcp-header` parameters."""

    def strip(value: Any) -> Any:
        if isinstance(value, dict):
            return {key: strip(item) for key, item in value.items() if key != "x-mcp-header"}
        if isinstance(value, list):
            return [strip(item) for item in value]
        return value

    return strip(schema)


class Adapter2025_11_25(Adapter):  # noqa: N801 -- revision date, greppable against the spec
    version: ClassVar[str] = "2025-11-25"
    has_handshake: ClassVar[bool] = True
    #: Elicitation starts in 2025-06-18; 2025-03-26 disables it.
    can_push_ask: ClassVar[bool] = True

    def params_model(self, operation: Operation) -> type[Params]:
        return PARAMS_MODELS.get(operation, Params)

    def client_info_for(self, params: Params) -> ClientInfo:
        if not isinstance(params, InitializeParams):
            return ClientInfo()
        info = params.client_info
        return ClientInfo(
            name=info.name if info is not None else None,
            version=info.version if info is not None else None,
            capabilities=params.capabilities,
        )

    def operation_for(self, method: str) -> Operation | None:
        return METHODS.get(method)

    def method_for(self, operation: Operation) -> str | None:
        return METHOD_NAMES.get(operation)

    def encode_value(self, call: Call, registry: RegistryProtocol, result: Any) -> dict[str, Any]:
        if isinstance(result, CallToolResult) and result.structured_content is not None:
            if not isinstance(result.structured_content, dict):
                result = result.model_copy(update={"structured_content": None})
        return {"jsonrpc": "2.0", "id": call.id, "result": result.wire()}

    def describe_server(self, registry: RegistryProtocol, call: Call) -> InitializeResult:
        requested = (
            call.params.protocol_version if isinstance(call.params, InitializeParams) else None
        )
        version = requested if requested in LEGACY_VERSIONS else self.version
        return InitializeResult(
            protocol_version=version,
            capabilities=self.capabilities(registry),
            server_info=registry.info,
            instructions=registry.instructions,
        )

    def describe_tool(self, spec: ToolSpec) -> ToolDef | None:
        if spec.min_revision is not None and spec.min_revision > self.version:
            return None
        schema = simplify_legacy_schema(spec.input_schema)
        if schema is None:
            return None
        schema = strip_header_annotations(schema)
        output_schema = spec.output_schema
        if output_schema is not None:
            output_schema = simplify_legacy_schema(output_schema)
        return spec.definition(schema, output_schema)

    def client_headers(
        self,
        method: str,
        name: str | None = None,
        params: Mapping[str, Any] | None = None,
        tool: ToolDef | None = None,
    ) -> Mapping[str, str]:
        return {"MCP-Protocol-Version": self.version}

    def client_handshake_params(self, client: ClientProfile) -> Params:
        return InitializeParams(
            protocol_version=self.version,
            client_info=client.info,
            capabilities=dict(client.capabilities),
        )
