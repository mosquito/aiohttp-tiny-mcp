"""Pydantic wire models for content, results, JSON-RPC envelopes, and request params."""

from __future__ import annotations

import json
import mimetypes
import re
from base64 import b64encode
from collections.abc import Mapping, Sequence
from enum import Enum
from math import isfinite
from pathlib import Path
from types import MappingProxyType
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    ValidationError,
    field_validator,
)
from pydantic.alias_generators import to_camel
from pydantic_core import to_json


def serialized(payload: Any) -> bytes:
    """Encode one wire payload as UTF-8 JSON.

    `to_json` is the serializer pydantic already carries. It is about three
    times faster than `json.dumps` and writes the same bytes for the JSON-mode
    values `Model.wire` returns. Non-ASCII characters stay as they are.
    """
    return to_json(payload)


class Model(BaseModel):
    """camelCase on the wire; unknown fields allowed for forward compatibility."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="allow")

    def wire(self) -> dict:
        return self.model_dump(mode="json", by_alias=True, exclude_none=True)


class Implementation(Model):
    name: str
    version: str
    title: str | None = None
    icons: Sequence[Icon] | None = None
    website_url: str | None = None


class TextContent(Model):
    type: Literal["text"] = "text"
    text: str


class ImageContent(Model):
    type: Literal["image"] = "image"
    data: str
    mime_type: str


class AudioContent(Model):
    type: Literal["audio"] = "audio"
    data: str
    mime_type: str


class ResourceLink(Model):
    type: Literal["resource_link"] = "resource_link"
    uri: str
    name: str
    description: str | None = None
    mime_type: str | None = None


class TextResourceContents(Model):
    uri: str
    text: str
    mime_type: str | None = None


class BlobResourceContents(Model):
    uri: str
    blob: str
    mime_type: str | None = None


class EmbeddedResource(Model):
    """Resource contents carried in the result, instead of a link to read later."""

    type: Literal["resource"] = "resource"
    resource: TextResourceContents | BlobResourceContents


ContentBlock = Annotated[
    TextContent | ImageContent | AudioContent | ResourceLink | EmbeddedResource,
    Field(discriminator="type"),
]


class ResultModel(Model):
    """resultType is required on 2026-07-28; legacy adapters leave it unset."""

    result_type: Literal["complete"] | None = Field(default=None, alias="resultType")
    meta: Meta | None = Field(default=None, alias="_meta")


def cache_ttl_milliseconds(seconds: int | float) -> int:
    """Convert a server TTL in seconds to whole wire milliseconds."""
    milliseconds = seconds * 1000
    if seconds < 0 or (isinstance(milliseconds, float) and not isfinite(milliseconds)):
        raise ValueError("cache TTL must be finite and non-negative")
    return int(milliseconds)


class CacheableResult(ResultModel):
    """2026-07-28 freshness hints (SEP-2549), omitted on older revisions."""

    ttl_ms: int | None = None
    cache_scope: Literal["private", "public"] | None = None


class CompletionMessage(Model):
    """One turn of the conversation a server sends to be completed."""

    role: Literal["user", "assistant"]
    content: TextContent | ImageContent | AudioContent


class CompletionRequest(Model):
    """A conversation and options for a client model completion."""

    messages: Sequence[CompletionMessage | Mapping[str, Any]]
    max_tokens: int
    system_prompt: str | None = None
    model_preferences: Mapping[str, Any] | None = None
    include_context: Literal["none", "thisServer", "allServers"] | None = None
    temperature: float | None = None
    stop_sequences: Sequence[str] | None = None
    metadata: Mapping[str, Any] | None = None

    def as_input(self) -> dict[str, Any]:
        """Build the server request envelope."""
        return {"method": "sampling/createMessage", "params": self.wire()}


class CompletionResult(Model):
    """The model-generated message returned to the handler.

    `model` names what produced it, which is the client's choice rather than the
    server's: a server states preferences and the client decides.
    """

    role: Literal["user", "assistant"]
    content: TextContent | ImageContent | AudioContent
    model: str
    stop_reason: str | None = None

    @classmethod
    def from_reply(cls, reply: Mapping[str, Any]) -> CompletionResult:
        """Parse a client reply. Raise ValueError if it contains no completion."""
        if not reply:
            raise ValueError("the client sent no answer to sampling/createMessage")
        if "code" in reply and "message" in reply:
            raise ValueError(f"the client refused the completion: {reply['message']}")
        try:
            return cls.model_validate(dict(reply))
        except ValidationError as error:
            raise ValueError(f"the client answered sampling/createMessage with: {error}") from None

    @property
    def text(self) -> str:
        """The text of a text completion, or an empty string for another kind."""
        return self.content.text if isinstance(self.content, TextContent) else ""


class Icon(Model):
    """An image a host may show beside a declaration (2025-11-25 and later).

    `src` is an `http:`, `https:` or `data:` URI. A `data:` URI carries the bytes in
    every listing that names it, and the server icon rides in the `_meta` of every
    result on 2026-07-28, so prefer a URL for anything but a small image. `sizes` holds
    entries of the form `WxH`, or `any` for one that scales.
    """

    src: str
    mime_type: str | None = None
    sizes: list[str] | None = None
    theme: Literal["dark", "light"] | None = None

    @classmethod
    def from_bytes(
        cls,
        data: bytes,
        *,
        mime_type: str,
        sizes: list[str] | None = None,
        theme: Literal["dark", "light"] | None = None,
    ) -> Icon:
        """Create an embedded icon from image bytes."""
        encoded = b64encode(data).decode("ascii")
        return cls(
            src=f"data:{mime_type};base64,{encoded}", mime_type=mime_type, sizes=sizes, theme=theme
        )

    @classmethod
    def from_file(
        cls,
        path: str | Path,
        *,
        mime_type: str | None = None,
        sizes: list[str] | None = None,
        theme: Literal["dark", "light"] | None = None,
    ) -> Icon:
        """Read an image file and embed it. Infer its MIME type from the filename."""
        path = Path(path)
        mime_type = mime_type or mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        return cls.from_bytes(path.read_bytes(), mime_type=mime_type, sizes=sizes, theme=theme)

    @classmethod
    def from_files(
        cls,
        light: str | Path,
        dark: str | Path,
        *,
        mime_type: str | None = None,
        sizes: list[str] | None = None,
    ) -> tuple[Icon, Icon]:
        """Read the light and dark variants of one icon."""
        return (
            cls.from_file(light, mime_type=mime_type, sizes=sizes, theme="light"),
            cls.from_file(dark, mime_type=mime_type, sizes=sizes, theme="dark"),
        )

    @field_validator("src")
    @classmethod
    def served_scheme(cls, value: str) -> str:
        """Allow the schemes a host fetches an image from, and refuse the rest.

        A `javascript:` or `file:` source is a problem for whoever renders it, and
        registration is the one place a declaration is checked.
        """
        if not value.startswith(("http://", "https://", "data:")):
            raise ValueError("an icon src must be an http:, https: or data: URI")
        return value

    @field_validator("sizes")
    @classmethod
    def stated_sizes(cls, value: list[str] | None) -> list[str] | None:
        """Each entry is `WxH`, or `any` for an image that scales."""
        for size in value or ():
            if size != "any" and not re.fullmatch(r"[1-9][0-9]*x[1-9][0-9]*", size):
                raise ValueError(f"an icon size must be 'any' or 'WxH', not {size!r}")
        return value


class Hint(Enum):
    """Tool annotations, combined with | and negated with ~.

        annotations=Hint.READ_ONLY | Hint.IDEMPOTENT | ~Hint.OPEN_WORLD

    DESTRUCTIVE and OPEN_WORLD default to true; READ_ONLY and IDEMPOTENT default to false. Hints
    inform hosts but are not enforced. Set the display title through registry.tool(title=...).
    """

    READ_ONLY = MappingProxyType({"readOnlyHint": True})
    DESTRUCTIVE = MappingProxyType({"destructiveHint": True})
    IDEMPOTENT = MappingProxyType({"idempotentHint": True})
    OPEN_WORLD = MappingProxyType({"openWorldHint": True})

    def keys(self):
        return self.value.keys()

    def __getitem__(self, key: str) -> bool:
        return self.value[key]

    def __or__(self, other: Any) -> Mapping[str, Any]:
        """Combine annotations; the right side wins on conflicts."""
        return MappingProxyType({**self, **other})

    def __ror__(self, other: Any) -> Mapping[str, Any]:
        return MappingProxyType({**other, **self})

    def __invert__(self) -> Mapping[str, Any]:
        """Negate this hint, including the true defaults DESTRUCTIVE and OPEN_WORLD."""
        return MappingProxyType({key: not value for key, value in self.value.items()})


class ToolDef(Model):
    name: str
    title: str | None = None
    # A server may omit the description, so a tool list stays readable without one.
    description: str = ""
    input_schema: dict[str, Any]
    output_schema: dict[str, Any] | None = None
    annotations: dict[str, Any] | None = None
    icons: Sequence[Icon] | None = None


class ListToolsResult(CacheableResult):
    tools: list[ToolDef]
    next_cursor: str | None = None


class CallToolResult(ResultModel):
    content: list[ContentBlock]
    structured_content: Any = None
    is_error: bool = False

    @classmethod
    def failure(cls, text: str) -> CallToolResult:
        return cls(content=[TextContent(text=text)], is_error=True)

    @classmethod
    def of(cls, value: Any) -> CallToolResult:
        """Coerce whatever a tool returned into a result."""
        match value:
            case cls():
                return value
            case str():
                return cls(content=[TextContent(text=value)])
            case BaseModel():
                data = value.model_dump(mode="json", by_alias=True)
                return cls(
                    content=[TextContent(text=json.dumps(data, ensure_ascii=False))],
                    structured_content=data,
                )
            case _:
                text = json.dumps(value, ensure_ascii=False, default=str)
                return cls(content=[TextContent(text=text)], structured_content=json.loads(text))


class ResourceDef(Model):
    uri: str
    name: str
    title: str | None = None
    description: str | None = None
    mime_type: str | None = None
    size: int | None = None
    icons: Sequence[Icon] | None = None


class ResourceTemplateDef(Model):
    uri_template: str
    name: str
    title: str | None = None
    description: str | None = None
    mime_type: str | None = None
    icons: Sequence[Icon] | None = None


class ListResourcesResult(CacheableResult):
    resources: list[ResourceDef]
    next_cursor: str | None = None


class ListResourceTemplatesResult(CacheableResult):
    resource_templates: list[ResourceTemplateDef]
    next_cursor: str | None = None


class ReadResourceResult(CacheableResult):
    contents: list[TextResourceContents | BlobResourceContents]


class PromptArgumentDef(Model):
    name: str
    description: str | None = None
    required: bool = False


class PromptDef(Model):
    name: str
    title: str | None = None
    description: str | None = None
    arguments: list[PromptArgumentDef] = Field(default_factory=list)
    icons: Sequence[Icon] | None = None


class ListPromptsResult(CacheableResult):
    prompts: list[PromptDef]
    next_cursor: str | None = None


class PromptMessage(Model):
    role: Literal["user", "assistant"]
    content: ContentBlock


class GetPromptResult(ResultModel):
    messages: list[PromptMessage]
    description: str | None = None


class Completion(Model):
    values: list[str]
    total: int | None = None
    has_more: bool = False


class CompleteResult(ResultModel):
    completion: Completion


class EmptyResult(ResultModel):
    pass


class Incoming(Model):
    jsonrpc: Literal["2.0"]
    method: str
    id: StrictStr | StrictInt | None = None
    params: dict[str, Any] = Field(default_factory=dict)

    @property
    def is_notification(self) -> bool:
        return "id" not in self.model_fields_set


class ErrorBody(Model):
    code: int
    message: str
    data: Any | None = None


class ErrorResponse(Model):
    jsonrpc: Literal["2.0"] = "2.0"
    id: str | int | None = None
    error: ErrorBody

    def wire(self) -> dict:
        payload = super().wire()
        # Parse/Invalid Request failures have an unknown request id. JSON-RPC
        # requires an explicit null here; omitting the member is not valid.
        payload["id"] = self.id
        return payload


class Meta(Model):
    """Wire _meta, aliased because Pydantic treats leading underscores as private."""

    protocol_version: str | None = Field(
        default=None, alias="io.modelcontextprotocol/protocolVersion"
    )
    client_info: Implementation | None = Field(
        default=None, alias="io.modelcontextprotocol/clientInfo"
    )
    client_capabilities: dict[str, Any] | None = Field(
        default=None, alias="io.modelcontextprotocol/clientCapabilities"
    )
    #: 2026-07-28 server identity, included in every result.
    server_info: Implementation | None = Field(
        default=None, alias="io.modelcontextprotocol/serverInfo"
    )
    progress_token: str | int | None = Field(default=None, alias="progressToken")
    log_level: str | None = Field(
        default=None, alias="io.modelcontextprotocol/logLevel"
    )  # draft, SEP-2575


# Resolve Meta before applications subclass these result bases in other modules.
ResultModel.model_rebuild()
CacheableResult.model_rebuild()


class Params(Model):
    meta: Meta = Field(default_factory=Meta, alias="_meta")
    # MRTR (SEP-2322).
    input_responses: dict[str, Any] | None = None
    request_state: str | None = None


class InitializeParams(Params):
    protocol_version: str
    capabilities: dict[str, Any] = Field(default_factory=dict)
    client_info: Implementation | None = None


class ListParams(Params):
    cursor: str | None = None


class MethodFilter(Model):
    """One extension multicast, narrowed to the topics named."""

    method: StrictStr
    topics: list[StrictStr] = Field(default_factory=list)


class ListenNotifications(Model):
    tools_list_changed: StrictBool = False
    prompts_list_changed: StrictBool = False
    resources_list_changed: StrictBool = False
    resource_subscriptions: list[StrictStr] = Field(default_factory=list)
    #: Extension broadcasts to relay: a method name for every event of it, or a
    #: `MethodFilter` for the topics of a multicast.
    methods: list[StrictStr | MethodFilter] = Field(default_factory=list)


class ListenParams(Params):
    notifications: ListenNotifications = Field(default_factory=ListenNotifications)


class CallToolParams(Params):
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class SetLevelParams(Params):
    """Session-level logging/setLevel; 2026-07-28 uses per-request metadata."""

    level: str


class SubscribeParams(Params):
    """Legacy resource subscription changes, accumulated in the session."""

    uri: str


class ReadResourceParams(Params):
    uri: str


class GetPromptParams(Params):
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class CompleteRef(Model):
    type: Literal["ref/prompt", "ref/resource"]
    name: str | None = None
    uri: str | None = None


class CompleteArgument(Model):
    name: str
    value: str


class CompleteParams(Params):
    ref: CompleteRef
    argument: CompleteArgument
    context: dict[str, Any] | None = None
