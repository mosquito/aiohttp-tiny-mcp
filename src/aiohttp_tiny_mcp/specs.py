"""Registration-time schemas, dependency plans, and URI-template matching."""

from __future__ import annotations

import base64
import inspect
import json
import re
from collections.abc import Awaitable, Callable, Iterable, Mapping
from contextlib import AbstractAsyncContextManager
from typing import Any, ClassVar, Literal, Protocol, get_type_hints

from pydantic import BaseModel, ConfigDict, Field

from .models import (
    BlobResourceContents,
    CallToolResult,
    Hint,
    Model,
    PromptArgumentDef,
    PromptDef,
    ResourceDef,
    ResourceTemplateDef,
    TextResourceContents,
    ToolDef,
)
from .schema import header_params, json_schema


class Resolver(Protocol):
    """What `Bound.call` needs from its caller."""

    def scope(self) -> AbstractAsyncContextManager[None]: ...

    async def resolve(self, kind: type) -> Any: ...


def described(given: str | None, fn: Callable[..., Any]) -> str:
    """Extract handler documentation without source indentation."""
    return inspect.cleandoc(given or fn.__doc__ or "").strip()


class Bound(Model):
    """A user function, its argument model and its dependency plan."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    fn: Callable[..., Awaitable[Any]]
    args_model: type[BaseModel]
    plan: tuple[tuple[str, type], ...]
    returns: Any = None

    @classmethod
    def of(cls, fn: Callable[..., Awaitable[Any]]) -> Bound:
        label = getattr(fn, "__name__", repr(fn))
        hints = get_type_hints(fn)
        returns = hints.pop("return", None)
        params = list(inspect.signature(fn).parameters.values())
        args_model = hints.get(params[0].name) if params else None
        if not (isinstance(args_model, type) and issubclass(args_model, BaseModel)):
            raise TypeError(f"{label}: first argument must be annotated with a BaseModel")
        plan = []
        for p in params[1:]:
            kind = hints.get(p.name)
            if kind is None:
                raise TypeError(
                    f"{label}: parameter {p.name!r} has no annotation, nothing to inject"
                )
            plan.append((p.name, kind))
        return cls(fn=fn, args_model=args_model, plan=tuple(plan), returns=returns)

    @property
    def structured(self) -> bool:
        return (
            isinstance(self.returns, type)
            and issubclass(self.returns, BaseModel)
            and not issubclass(self.returns, CallToolResult)
        )

    async def call(self, arguments: dict[str, Any], ex: Resolver) -> Any:
        args = self.args_model.model_validate(arguments)
        async with ex.scope():
            deps = {name: await ex.resolve(kind) for name, kind in self.plan}
            return await self.fn(args, **deps)


class ToolSpec(Model):
    """Full tool definition, simplified by adapters for older revisions."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    name: str
    title: str | None = None
    description: str
    bound: Bound
    input_schema: dict[str, Any]
    output_schema: dict[str, Any] | None = None
    header_params: tuple[tuple[tuple[str, ...], str], ...] = ()
    annotations: Mapping[str, Any] = Field(default_factory=dict)
    scopes: frozenset[str] = Field(default_factory=frozenset)
    min_revision: str | None = None
    streaming: bool = False

    @classmethod
    def build(
        cls,
        fn: Callable[..., Awaitable[Any]],
        *,
        name: str | None = None,
        title: str | None = None,
        description: str | None = None,
        annotations: Mapping[str, Any] | Hint | None = None,
        scopes: Iterable[str] | None = None,
        min_revision: str | None = None,
        streaming: bool = False,
    ) -> ToolSpec:
        bound = Bound.of(fn)
        name = name or getattr(fn, "__name__", repr(fn))
        schema = json_schema(bound.args_model)
        return cls(
            name=name,
            title=title,
            description=described(description, fn),
            bound=bound,
            input_schema=schema,
            output_schema=json_schema(bound.returns) if bound.structured else None,
            header_params=header_params(schema),
            annotations=dict(annotations or {}),
            scopes=frozenset(scopes or ()),
            min_revision=min_revision,
            streaming=streaming,
        )

    def definition(self, schema: dict[str, Any], output_schema: dict[str, Any] | None) -> ToolDef:
        return ToolDef(
            name=self.name,
            title=self.title,
            description=self.description,
            input_schema=schema,
            output_schema=output_schema,
            annotations=dict(self.annotations) or None,
        )


class ResourceSpec(Model):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    bound: Bound
    definition: ResourceDef | None = None  # fixed URI
    template: ResourceTemplateDef | None = None  # URI template
    pattern: Any = None  # compiled matcher for the template
    mime_type: str | None = None
    cache_ttl_ms: int | None = None
    cache_scope: Literal["public", "private"] | None = None
    legacy_uri: str | None = None
    legacy_pattern: Any = None
    legacy_only: bool = False

    VAR: ClassVar[re.Pattern] = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")

    @classmethod
    def build(
        cls,
        uri: str,
        fn: Callable[..., Awaitable[Any]],
        *,
        name: str | None = None,
        title: str | None = None,
        description: str | None = None,
        mime_type: str | None = None,
        cache_ttl_ms: int | None = None,
        cache_scope: Literal["public", "private"] | None = None,
    ) -> ResourceSpec:
        bound = Bound.of(fn)
        name = name or getattr(fn, "__name__", repr(fn))
        description = described(description, fn) or None
        if not cls.VAR.search(uri):
            return cls(
                bound=bound,
                mime_type=mime_type,
                cache_ttl_ms=cache_ttl_ms,
                cache_scope=cache_scope,
                definition=ResourceDef(
                    uri=uri, name=name, title=title, description=description, mime_type=mime_type
                ),
            )
        parts, last = [], 0
        for m in cls.VAR.finditer(uri):
            parts.append(re.escape(uri[last : m.start()]))
            parts.append(f"(?P<{m.group(1)}>[^/]+)")
            last = m.end()
        parts.append(re.escape(uri[last:]))
        return cls(
            bound=bound,
            mime_type=mime_type,
            cache_ttl_ms=cache_ttl_ms,
            cache_scope=cache_scope,
            pattern=re.compile("^" + "".join(parts) + "$"),
            template=ResourceTemplateDef(
                uri_template=uri,
                name=name,
                title=title,
                description=description,
                mime_type=mime_type,
            ),
        )

    def contents(self, uri: str, value: Any) -> list[Any]:
        match value:
            case TextResourceContents() | BlobResourceContents():
                if self.legacy_uri is not None:
                    return [value.model_copy(update={"uri": uri})]
                return [value]
            case bytes():
                return [
                    BlobResourceContents(
                        uri=uri,
                        blob=base64.b64encode(value).decode(),
                        mime_type=self.mime_type or "application/octet-stream",
                    )
                ]
            case str():
                return [
                    TextResourceContents(
                        uri=uri, text=value, mime_type=self.mime_type or "text/plain"
                    )
                ]
            case BaseModel():
                value = value.model_dump(mode="json", by_alias=True)
        return [
            TextResourceContents(
                uri=uri,
                mime_type=self.mime_type or "application/json",
                text=json.dumps(value, ensure_ascii=False, default=str),
            )
        ]


class PromptSpec(Model):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    name: str
    bound: Bound
    definition: PromptDef

    @classmethod
    def build(
        cls,
        fn: Callable[..., Awaitable[Any]],
        *,
        name: str | None = None,
        title: str | None = None,
        description: str | None = None,
    ) -> PromptSpec:
        bound = Bound.of(fn)
        name = name or getattr(fn, "__name__", repr(fn))
        schema = json_schema(bound.args_model)
        required = set(schema.get("required", []))
        return cls(
            name=name,
            bound=bound,
            definition=PromptDef(
                name=name,
                title=title,
                description=described(description, fn) or None,
                arguments=[
                    PromptArgumentDef(
                        name=k, description=v.get("description"), required=k in required
                    )
                    for k, v in schema.get("properties", {}).items()
                ],
            ),
        )
