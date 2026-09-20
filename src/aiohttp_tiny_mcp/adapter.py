"""Protocol adapters and structural interfaces for requests and registries."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from types import MappingProxyType
from typing import Any, ClassVar, Protocol, cast

from pydantic import BaseModel, ValidationError

from .core import (
    AnswerAction,
    Call,
    ClientInfo,
    ClientProfile,
    DecodeFailure,
    Failure,
    FailureKind,
    InputRequest,
    NeedsInput,
    Operation,
    Preamble,
    Rejected,
    Value,
    answer_actions,
    decode_failure_target,
)
from .extensions import ExtensionSpec
from .hub import Hub
from .models import (
    CallToolParams,
    CompleteParams,
    ErrorBody,
    ErrorResponse,
    GetPromptParams,
    Implementation,
    Incoming,
    ListParams,
    Params,
    PromptDef,
    ReadResourceParams,
    ResourceDef,
    ResourceTemplateDef,
    ToolDef,
)
from .specs import Bound, PromptSpec, ResourceSpec, ToolSpec


class RequestLike(Protocol):
    """Request interface for AppKey-based dependency injection, including non-HTTP transports."""

    @property
    def app(self) -> Any: ...


class RegistryProtocol(Protocol):
    """Read-only properties allow registries with concrete dict collections to conform."""

    @property
    def info(self) -> Implementation: ...
    @property
    def instructions(self) -> str | None: ...
    @property
    def hub(self) -> Hub: ...
    @property
    def tools(self) -> Mapping[str, ToolSpec]: ...
    @property
    def resources_fixed(self) -> Mapping[str, ResourceSpec]: ...
    @property
    def resources_templated(self) -> Sequence[ResourceSpec]: ...
    @property
    def prompts(self) -> Mapping[str, PromptSpec]: ...
    @property
    def completer(self) -> Bound | None: ...
    @property
    def extensions(self) -> Mapping[str, ExtensionSpec]: ...
    def match_resource(self, uri: str) -> tuple[ResourceSpec, dict[str, str]] | None: ...


class Adapter(ABC):
    """One instance per revision, stateless and shared across requests."""

    version: ClassVar[str]
    supersedes: ClassVar[tuple[str, ...]] = ()

    BASE_FAILURE_MAP: ClassVar[Mapping[FailureKind, tuple[int, int]]] = MappingProxyType(
        {
            FailureKind.PARSE: (-32700, 400),
            FailureKind.MALFORMED: (-32600, 400),
            FailureKind.INVALID_PARAMS: (-32602, 200),
            FailureKind.ORIGIN_REJECTED: (-32600, 403),
            FailureKind.SESSION_NOT_FOUND: (-32001, 404),
            FailureKind.INTERNAL: (-32603, 200),
            FailureKind.INVALID_ARGUMENTS: (-32603, 200),
            FailureKind.UNKNOWN_METHOD: (-32601, 200),
            FailureKind.UNKNOWN_TARGET: (-32601, 200),
            FailureKind.RESOURCE_NOT_FOUND: (-32002, 200),
            FailureKind.HEADER_MISMATCH: (-32603, 200),
            FailureKind.UNSUPPORTED_VERSION: (-32600, 400),
            FailureKind.INPUT_UNSUPPORTED: (-32603, 200),
            FailureKind.MISSING_REQUIRED_CAPABILITY: (-32603, 200),
        }
    )
    FAILURE_MAP: ClassVar[Mapping[FailureKind, tuple[int, int]]] = BASE_FAILURE_MAP

    def bind(self, versions: tuple[str, ...]) -> None:
        """Receive the served revisions once at AdapterSet construction, for discovery."""
        return None  # noqa: B027 -- concrete default, most revisions need nothing

    def check_http(
        self,
        pre: Preamble,
        headers: Mapping[str, str],
        registry: RegistryProtocol | None = None,
    ) -> None:
        """Raise `Rejected` on a revision-specific HTTP binding violation."""
        return None  # noqa: B027 -- concrete default, most revisions check nothing

    def decode(
        self, pre: Preamble, registry: RegistryProtocol | None = None
    ) -> Sequence[Call | DecodeFailure]:
        """Decode messages independently; one invalid item does not abort the batch."""
        if pre.parse_error:
            raise Rejected(Failure(FailureKind.PARSE, "invalid JSON body"))
        if pre.is_batch:
            if not self.allows_batch:
                raise Rejected(Failure(FailureKind.MALFORMED, "batching not supported"))
            if not pre.body:  # `is_batch` already means the body is a list
                raise Rejected(Failure(FailureKind.MALFORMED, "empty batch"))
            return [self.decode_item(item, registry) for item in pre.body]
        return [self.decode_item(pre.body, registry)]

    def decode_item(
        self, item: object, registry: RegistryProtocol | None = None
    ) -> Call | DecodeFailure:
        """Preserve the request id on failure without aborting sibling batch items."""
        if not isinstance(item, dict):
            return DecodeFailure(
                id=None,
                failure=Failure(FailureKind.MALFORMED, "request must be an object"),
                must_respond=True,
            )
        body = cast(dict[str, Any], item)
        try:
            return self.decode_one(body, registry)
        except Rejected as e:
            call_id, must_respond = decode_failure_target(body)
            return DecodeFailure(id=call_id, failure=e.failure, must_respond=must_respond)

    def decode_one(self, body: dict[str, Any], registry: RegistryProtocol | None = None) -> Call:
        if "id" in body and body["id"] is None:
            raise Rejected(Failure(FailureKind.MALFORMED, "id must not be null"))
        try:
            msg = Incoming.model_validate(body)
        except ValidationError as e:
            raise Rejected(Failure(FailureKind.MALFORMED, str(e))) from None
        operation = self.operation_for(msg.method)
        if operation is None and registry is not None and self.supports_extensions:
            if any(
                msg.method in spec.methods and spec.min_revision <= self.version
                for spec in registry.extensions.values()
            ):
                operation = Operation.EXTENSION
        if operation is None:
            raise Rejected(Failure(FailureKind.UNKNOWN_METHOD, f"unknown method: {msg.method}"))
        self.check_message(operation, msg)
        try:
            params = self.params_model(operation).model_validate(msg.params)
        except ValidationError as e:
            raise Rejected(Failure(FailureKind.INVALID_PARAMS, str(e))) from None
        self.check_params(params)
        call = self.build_call(operation, msg, params)
        if operation is Operation.EXTENSION:
            if msg.is_notification:
                raise Rejected(Failure(FailureKind.MALFORMED, "extension requests require an id"))
            call.target = msg.method
            call.arguments = {key: value for key, value in msg.params.items() if key != "_meta"}
        return call

    def build_call(self, operation: Operation, msg: Incoming, params: Params) -> Call:
        target: str | None = None
        arguments: dict[str, Any] = {}
        match params:
            case CallToolParams() | GetPromptParams():
                target, arguments = params.name, dict(params.arguments)
            case ReadResourceParams():
                target = params.uri
            case CompleteParams():
                arguments = params.model_dump(
                    by_alias=True, exclude={"meta", "input_responses", "request_state"}
                )
            case ListParams():
                arguments = {"cursor": params.cursor} if params.cursor else {}
        return Call(
            operation=operation,
            id=msg.id,
            target=target,
            arguments=arguments,
            params=params,
            client=self.client_info_for(params),
            progress_token=params.meta.progress_token,
            log_level=params.meta.log_level,
            answers=self.answers_for(params),
            actions=self.actions_for(params),
            state=params.request_state,
            raw=msg.params,
            is_notification=msg.is_notification,
        )

    def params_model(self, operation: Operation) -> type[Params]:
        """The params model this revision validates `operation` against."""
        return Params

    def check_message(self, operation: Operation, msg: Incoming) -> None:
        """Reject an envelope this revision does not allow for `operation`."""
        return None  # noqa: B027 -- concrete default, most revisions check nothing

    def check_params(self, params: Params) -> None:
        """Reject params this revision requires more of (2026-07-28 `_meta`)."""
        return None  # noqa: B027 -- concrete default, most revisions check nothing

    def client_info_for(self, params: Params) -> ClientInfo:
        """Client identity, if the revision supplies it."""
        return ClientInfo()

    def answers_for(self, params: Params) -> Mapping[str, Any]:
        """MRTR answers, as this revision carries them."""
        return params.input_responses or {}

    def actions_for(self, params: Params) -> Mapping[str, AnswerAction]:
        """Whether each answer accepted, declined, or cancelled its request."""
        return answer_actions(params.input_responses)

    @abstractmethod
    def operation_for(self, method: str) -> Operation | None: ...

    @abstractmethod
    def method_for(self, operation: Operation) -> str | None: ...

    def encode(
        self, call: Call, registry: RegistryProtocol, outcome: Value | NeedsInput | Failure
    ) -> Mapping[str, Any]:
        """Encode a final response, identically for JSON, SSE, and stdio."""
        match outcome:
            case Value():
                return self.encode_value(call, registry, outcome.result)
            case NeedsInput():
                return self.encode_input_required(call, registry, outcome)
            case Failure():
                return self.encode_failure(call.id, outcome)

    @abstractmethod
    def encode_value(
        self, call: Call, registry: RegistryProtocol, result: BaseModel
    ) -> Mapping[str, Any]: ...

    def encode_input_required(
        self, call: Call, registry: RegistryProtocol, out: NeedsInput
    ) -> Mapping[str, Any]:
        raise NotImplementedError(f"{self.version} cannot carry MRTR")

    def encode_failure(self, call_id: Any, failure: Failure) -> Mapping[str, Any]:
        code, _ = self.FAILURE_MAP[failure.kind]
        return ErrorResponse(
            id=call_id, error=ErrorBody(code=code, message=failure.message, data=failure.data)
        ).wire()

    @property
    def carries_state(self) -> bool:
        """Client-held state must be sealed on output and verified on return."""
        return self.can_ask or self.asks_in_arguments

    def http_status(self, failure: Failure) -> int:
        return self.FAILURE_MAP[failure.kind][1]

    def client_headers(
        self,
        method: str,
        name: str | None = None,
        params: Mapping[str, Any] | None = None,
        tool: ToolDef | None = None,
    ) -> Mapping[str, str]:
        """Required HTTP headers; `name` identifies the tool, resource, or prompt."""
        return {}

    def client_handshake_params(self, client: ClientProfile) -> Params:
        """Params for `initialize` or `server/discover`."""
        return Params()

    def client_decorate_params(self, params: Params, client: ClientProfile) -> Params:
        """Apply revision-specific metadata before sending a request."""
        return params

    def client_input_requests(
        self, result: Mapping[str, Any]
    ) -> tuple[Mapping[str, InputRequest], Any] | None:
        """Return input requests and retry state, or None. Stream-pushed requests bypass this."""
        return None

    def capabilities(self, registry: RegistryProtocol) -> Mapping[str, Any]:
        """Capabilities shared by all supported revisions."""
        caps: dict[str, Any] = {}
        if registry.tools:
            caps["tools"] = {"listChanged": True}
        if any(
            self.describe_resource(spec) is not None
            for spec in (*registry.resources_fixed.values(), *registry.resources_templated)
        ):
            caps["resources"] = {"listChanged": True, "subscribe": True}
        if registry.prompts:
            caps["prompts"] = {"listChanged": True}
        if registry.completer is not None:
            caps["completions"] = {}
        caps["logging"] = {}
        return caps

    @abstractmethod
    def describe_server(self, registry: RegistryProtocol, call: Call) -> BaseModel:
        """Negotiate the requested legacy revision from `call.params.protocol_version`."""

    @abstractmethod
    def describe_tool(self, spec: ToolSpec) -> ToolDef | None: ...

    def describe_resource(self, spec: ResourceSpec) -> ResourceDef | ResourceTemplateDef | None:
        """Project extension resources onto the URI understood by this revision."""
        if spec.legacy_only and self.supports_extensions:
            return None
        if spec.definition is not None and spec.legacy_uri and not self.supports_extensions:
            return spec.definition.model_copy(update={"uri": spec.legacy_uri})
        if spec.template is not None and spec.legacy_uri and not self.supports_extensions:
            return spec.template.model_copy(update={"uri_template": spec.legacy_uri})
        return spec.definition if spec.definition is not None else spec.template

    def describe_prompt(self, spec: PromptSpec) -> PromptDef | None:
        """Prompt definitions are shared across revisions."""
        return spec.definition

    #: MRTR: return questions, then retry with answers.
    can_ask: ClassVar[bool] = False
    #: Push questions on the active stream; receive answers in a separate POST.
    can_push_ask: ClassVar[bool] = False
    #: Pre-elicitation fallback: exchange questions and answers through tool calls.
    asks_in_arguments: ClassVar[bool] = False
    #: Whether a progress notification may carry a human-readable `message`.
    #: It arrived in 2025-03-26.
    progress_message: ClassVar[bool] = True
    allows_batch: ClassVar[bool] = False
    #: Negotiate once via initialize; retain the revision and capabilities in a session.
    has_handshake: ClassVar[bool] = False
    supports_extensions: ClassVar[bool] = False
