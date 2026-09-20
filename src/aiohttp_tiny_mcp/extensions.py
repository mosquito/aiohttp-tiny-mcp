"""Reusable extension declarations for MCP 2026-07-28 and later."""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Awaitable, Callable, Iterable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any
from urllib.parse import unquote

from pydantic import BaseModel

from .core import Failure, FailureKind, Rejected
from .models import ResultModel
from .schema import json_schema
from .specs import Bound, ResourceSpec

RESERVED_NOTIFICATIONS = frozenset(
    {
        "notifications/cancelled",
        "notifications/initialized",
        "notifications/message",
        "notifications/progress",
        "notifications/prompts/list_changed",
        "notifications/resources/list_changed",
        "notifications/resources/updated",
        "notifications/roots/list_changed",
        "notifications/subscriptions/acknowledged",
        "notifications/tools/list_changed",
    }
)


def check_notification(name: str, field: str | None) -> None:
    """Refuse a broadcast name the protocol owns or a client could not route, and a topic
    field a listener could not match on.
    """
    if (
        not name.startswith("notifications/")
        or len(name) <= len("notifications/")
        or any(char.isspace() for char in name)
        or any(part in ("", ".", "..") for part in name.split("/"))
    ):
        raise ValueError(f"invalid broadcast notification: {name!r}")
    if name in RESERVED_NOTIFICATIONS:
        raise ValueError(f"extension cannot broadcast a protocol notification: {name}")
    if field is not None and (not isinstance(field, str) or not field or field == "_meta"):
        raise ValueError(f"invalid topic field for {name}: {field!r}")


@dataclass(frozen=True)
class ExtensionSpec:
    capabilities: Mapping[str, Any]
    min_revision: str
    methods: Mapping[str, Bound]
    #: Broadcast method to the params field that carries its topic, or None for one
    #: every listener of the method gets.
    notifications: Mapping[str, str | None] = dataclasses.field(default_factory=dict)


class ExtensionResult(ResultModel):
    """Preserve result discriminators defined by an extension."""

    result_type: str | None = None


class Nothing(BaseModel):
    pass


class LegacyParams(BaseModel):
    params: str = "{}"


def map_resource_uris(value: Any, resources: list[ResourceSpec], *, legacy: bool) -> Any:
    """Translate URI fields without changing opaque cursors or file contents."""
    if isinstance(value, list):
        return [map_resource_uris(item, resources, legacy=legacy) for item in value]
    if not isinstance(value, dict):
        return value
    result = {}
    for key, item in value.items():
        if key == "uri" and isinstance(item, str):
            for spec in resources:
                assert spec.legacy_uri is not None
                if spec.definition is not None:
                    canonical = spec.definition.uri
                else:
                    assert spec.template is not None
                    canonical = spec.template.uri_template
                source, target = (
                    (canonical, spec.legacy_uri) if legacy else (spec.legacy_uri, canonical)
                )
                pattern = spec.pattern if legacy else spec.legacy_pattern
                if item == source:
                    item = target
                    break
                if pattern is not None and (match := pattern.fullmatch(item)):
                    item = ResourceSpec.VAR.sub(lambda m: match[m[1]], target)
                    break
            result[key] = item
        else:
            result[key] = map_resource_uris(item, resources, legacy=legacy)
    return result


class Extension:
    """Bundle methods, capability settings, and resources for registration.

    Configure the extension before passing it to ``registry.extension()``.
    Each registry takes a snapshot of the declarations.
    """

    def __init__(
        self,
        name: str,
        *,
        capabilities: Mapping[str, Any] | None = None,
        min_revision: str = "2026-07-28",
        notifications: Iterable[str] | Mapping[str, str | None] = (),
    ) -> None:
        if (
            not name
            or "/" not in name
            or any(part in ("", ".", "..") for part in name.split("/"))
            or any(char.isspace() or char in "?#%:\\" for char in name)
        ):
            raise ValueError("extension name must be a namespaced identifier")
        if min_revision < "2026-07-28":
            raise ValueError("extensions require MCP 2026-07-28 or later")
        self.name = name
        self.capabilities = deepcopy(dict(capabilities or {}))
        self.min_revision = min_revision
        self.methods: dict[str, Bound] = {}
        self.resources: dict[str, ResourceSpec] = {}
        #: Methods `Registry.broadcast` may send, each with the params field that
        #: carries its topic. None makes it a broadcast every listener of the method
        #: gets; a field makes it a multicast a listener filters by topic.
        self.notifications: dict[str, str | None] = {}
        declared = (
            notifications.items()
            if isinstance(notifications, Mapping)
            else ((method, None) for method in notifications)
        )
        for method, field in declared:
            check_notification(method, field)
            self.notifications[method] = field

    def method(self, name: str, fn: Callable[..., Awaitable[Any]] | None = None):
        """Register an async handler whose first argument is a Pydantic model.

        Return a result mapping or a ``ResultModel`` subclass. Additional
        parameters use the registry's dependency providers, including Exchange.
        Older revisions invoke the same handler through a namespaced resource.
        Parameters use a percent-encoded JSON query value. Reading a method
        resource can change state, depending on the handler.
        """
        if not name or name.startswith("rpc.") or any(char.isspace() for char in name):
            raise ValueError(f"invalid extension method: {name!r}")
        if (
            any(part in ("", ".", "..") for part in name.split("/"))
            or any(char in "?#%:{}\\" for char in name)
            or name == "manifest.json"
        ):
            raise ValueError(f"invalid legacy method path: {name!r}")

        def register(fn: Callable[..., Awaitable[Any]]):
            if name in self.methods:
                raise ValueError(f"duplicate extension method: {name}")
            self.methods[name] = Bound.of(fn)
            return fn

        return register(fn) if fn is not None else register

    def resource(
        self,
        uri: str,
        fn: Callable[..., Awaitable[Any]] | None = None,
        *,
        legacy_path: str | None = None,
        **kw: Any,
    ):
        """Bundle a resource with a legacy ``mcp-extensions://{name}/...`` URI.

        By default, the legacy path is the URI after its scheme. Set
        ``legacy_path`` to choose a different relative path within the extension.
        """
        path = legacy_path if legacy_path is not None else uri.split("://", 1)[-1]
        if not path or any(part in ("", ".", "..") for part in path.split("/")):
            raise ValueError("legacy_path must be a relative resource path")
        if path == "manifest.json":
            raise ValueError("manifest.json is reserved for the extension manifest")

        def register(fn: Callable[..., Awaitable[Any]]):
            if uri in self.resources:
                raise ValueError(f"duplicate extension resource: {uri}")
            spec = ResourceSpec.build(uri, fn, **kw)
            spec.legacy_uri = f"mcp-extensions://{self.name}/{path}"
            alias = ResourceSpec.build(spec.legacy_uri, fn)
            if set(ResourceSpec.VAR.findall(uri)) != set(ResourceSpec.VAR.findall(path)):
                raise ValueError("legacy_path must use the same template variables as the URI")
            spec.legacy_pattern = alias.pattern
            self.resources[uri] = spec
            return fn

        return register(fn) if fn is not None else register

    def method_resources(self) -> tuple[dict[str, ResourceSpec], dict[str, Any]]:
        """Build legacy resource routes around every registered method."""
        from .exchange import Exchange

        resources = [spec.model_copy(deep=True) for spec in self.resources.values()]
        routes: dict[str, ResourceSpec] = {}
        entries = {}

        def reader(bound: Bound):
            async def read(args: LegacyParams, ex) -> dict:
                try:
                    params = json.loads(unquote(args.params))
                except (ValueError, UnicodeError) as error:
                    raise Rejected(
                        Failure(FailureKind.INVALID_PARAMS, "Invalid JSON params")
                    ) from error
                if not isinstance(params, dict):
                    raise Rejected(Failure(FailureKind.INVALID_PARAMS, "params must be an object"))
                params = map_resource_uris(params, resources, legacy=False)
                value = await bound.call(params, ex)
                if isinstance(value, BaseModel):
                    value = value.model_dump(mode="json", by_alias=True, exclude_none=True)
                if not isinstance(value, Mapping):
                    raise TypeError("extension handler must return a result object")
                return map_resource_uris(dict(value), resources, legacy=True)

            read.__annotations__["ex"] = Exchange
            return read

        for name in sorted(self.methods):
            bound = self.methods[name].model_copy(deep=True)
            uri = f"mcp-extensions://{self.name}/{name}"
            schema = json_schema(bound.args_model)
            entry = {
                "uriTemplate": uri + "?params={params}",
                "inputSchema": schema,
                "description": (bound.fn.__doc__ or "").strip(),
            }
            if isinstance(bound.returns, type) and issubclass(bound.returns, BaseModel):
                entry["outputSchema"] = json_schema(bound.returns)
            if not schema.get("required"):
                entry["uri"] = uri
            handler = reader(bound)
            for address in [entry["uriTemplate"], *([uri] if "uri" in entry else [])]:
                spec = ResourceSpec.build(
                    address,
                    handler,
                    name=name,
                    mime_type="application/json",
                    description=(
                        f"Invoke {name}; this can change state. "
                        "params is a percent-encoded JSON object. "
                        + (bound.fn.__doc__ or "").strip()
                    ),
                )
                spec.legacy_only = True
                routes[address] = spec
            entries[name] = entry
        return routes, entries

    def manifest_resource(
        self, routes: dict[str, ResourceSpec] | None = None, methods: dict[str, Any] | None = None
    ) -> ResourceSpec:
        """Describe the extension to clients that only support resources."""
        manifest = {
            "name": self.name,
            "instructions": (
                "Use resources/read to invoke a method URI. For arguments, substitute "
                "percent-encoded JSON for {params} in its uriTemplate. Each read invokes "
                "the handler and can change state. Results are JSON resource contents; "
                "failures are JSON-RPC errors. Follow returned resource URIs to read files."
            ),
            "capabilities": self.capabilities,
            "methods": sorted(self.methods),
            "notifications": {
                name: self.notifications[name] for name in sorted(self.notifications)
            },
            "resources": sorted(
                [
                    spec.legacy_uri
                    for spec in self.resources.values()
                    if spec.legacy_uri is not None and spec.definition is not None
                ]
                + [uri for uri, spec in (routes or {}).items() if spec.definition is not None]
            ),
        }
        templates = sorted(
            [
                spec.legacy_uri
                for spec in self.resources.values()
                if spec.template is not None and spec.legacy_uri is not None
            ]
            + [uri for uri, spec in (routes or {}).items() if spec.template is not None]
        )
        if templates:
            manifest["resourceTemplates"] = templates
        if methods:
            manifest["methodResources"] = methods
        content = json.dumps(manifest, ensure_ascii=False)

        async def read(args: Nothing) -> str:
            return content

        spec = ResourceSpec.build(
            f"mcp-extensions://{self.name}/manifest.json",
            read,
            name=f"{self.name} manifest",
            mime_type="application/json",
            description="Compatibility resource routes, parameter schemas, and file URIs.",
        )
        spec.legacy_only = True
        return spec

    def snapshot(self) -> ExtensionSpec:
        return ExtensionSpec(
            capabilities=MappingProxyType(deepcopy(self.capabilities)),
            min_revision=self.min_revision,
            methods=MappingProxyType(
                {name: bound.model_copy(deep=True) for name, bound in self.methods.items()}
            ),
            notifications=MappingProxyType(dict(self.notifications)),
        )
