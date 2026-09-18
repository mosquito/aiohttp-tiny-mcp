"""Reusable extension declarations for MCP 2026-07-28 and later."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from pydantic import BaseModel

from .specs import Bound, ResourceSpec


@dataclass(frozen=True)
class ExtensionSpec:
    capabilities: Mapping[str, Any]
    min_revision: str
    methods: Mapping[str, Bound]


class Nothing(BaseModel):
    pass


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

    def method(self, name: str, fn: Callable[..., Awaitable[Any]] | None = None):
        """Register an async handler whose first argument is a Pydantic model.

        Return a result mapping or a ``ResultModel`` subclass. Additional
        parameters use the registry's dependency providers, including Exchange.
        """
        if not name or name.startswith("rpc.") or any(char.isspace() for char in name):
            raise ValueError(f"invalid extension method: {name!r}")

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
        """Bundle a resource with a legacy ``mcp-extensions/{name}/...`` URI.

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
            if spec.definition is None:
                raise ValueError("extension resources must use fixed URIs")
            spec.legacy_uri = f"mcp-extensions/{self.name}/{path}"
            self.resources[uri] = spec
            return fn

        return register(fn) if fn is not None else register

    def manifest_resource(self) -> ResourceSpec:
        """Describe the extension to clients that only support resources."""
        content = json.dumps(
            {
                "name": self.name,
                "capabilities": self.capabilities,
                "methods": sorted(self.methods),
                "resources": sorted(
                    spec.legacy_uri
                    for spec in self.resources.values()
                    if spec.legacy_uri is not None
                ),
            },
            ensure_ascii=False,
        )

        async def read(args: Nothing) -> str:
            return content

        spec = ResourceSpec.build(
            f"mcp-extensions/{self.name}/manifest.json",
            read,
            name=f"{self.name} manifest",
            mime_type="application/json",
            description=(
                f"Extension metadata and resource URIs. Methods require MCP {self.min_revision}+."
            ),
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
        )
