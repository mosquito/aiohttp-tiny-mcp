"""Public API: register tools, resources, prompts, and extensions."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable, Mapping
from typing import Any

from aiohttp_tiny_mcp.auth import Authentication, Principal
from aiohttp_tiny_mcp.extensions import Extension, ExtensionSpec
from aiohttp_tiny_mcp.protocol.models import Implementation
from aiohttp_tiny_mcp.storage.hub import NOTIFICATIONS, Cursor, Hub, MemoryHub, topic
from aiohttp_tiny_mcp.storage.sessions import DEFAULT_TTL_SECONDS, MemorySessionStore, SessionStore

from .exchange import Exchange, Instance
from .request_state import DEFAULT_TTL_SECONDS as STATE_TTL_SECONDS
from .request_state import RequestStates
from .specs import Bound, PromptSpec, ResourceSpec, ToolSpec


class Registry:
    """Register handlers as decorators or by passing functions directly.

        @registry.tool
        async def add(args: Add) -> int: ...

        registry.tool(add, name="sum")
        registry.resource("config://app", config)

    Uses process-local memory backends by default. Supply shared `hub` and
    `session_store` implementations before running more than one worker.
    """

    def __init__(
        self,
        name: str,
        version: str,
        *,
        hub: Hub | None = None,
        session_store: SessionStore | None = None,
        auth: Authentication | Iterable[Authentication] | None = None,
        instructions: str | None = None,
        session_ttl_seconds: int = DEFAULT_TTL_SECONDS,
        request_state_ttl_seconds: int = STATE_TTL_SECONDS,
        hub_poll_seconds: float = 30.0,
        ask_timeout_seconds: float = 120.0,
        page_size: int | None = None,
    ) -> None:
        self.info = Implementation(name=name, version=version)
        self.instructions = instructions
        self.hub = hub if hub is not None else MemoryHub()
        self.session_store = session_store if session_store is not None else MemorySessionStore()
        self.auth = auth
        self.session_ttl_seconds = session_ttl_seconds
        #: Most entries per listing page. `None`, the default, answers with all of them.
        self.page_size = page_size
        self.request_state = RequestStates(self.session_store, request_state_ttl_seconds)
        self.hub_poll_seconds = hub_poll_seconds
        self.ask_timeout_seconds = ask_timeout_seconds
        self.tools: dict[str, ToolSpec] = {}
        self.resources_fixed: dict[str, ResourceSpec] = {}
        self.resources_templated: list[ResourceSpec] = []
        self.prompts: dict[str, PromptSpec] = {}
        self.completer: Bound | None = None
        self.providers: dict[type, Any] = {}
        self.extensions: dict[str, ExtensionSpec] = {}
        self.resource_aliases: dict[str, ResourceSpec] = {}

    @property
    def auth(self) -> Authentication | tuple[Authentication, ...] | None:
        """Configured authentication policies. Iterables are consumed once."""
        return self._auth

    @auth.setter
    def auth(self, value: Authentication | Iterable[Authentication] | None) -> None:
        if value is None or isinstance(value, Authentication):
            self._auth = value
            return
        policies = tuple(value)
        if not policies or not all(isinstance(policy, Authentication) for policy in policies):
            raise ValueError("auth must contain at least one Authentication policy")
        self._auth = policies

    @property
    def auth_policies(self) -> tuple[Authentication, ...]:
        """Authentication alternatives in registration order."""
        if self._auth is None:
            return ()
        if isinstance(self._auth, Authentication):
            return (self._auth,)
        return self._auth

    def extension(self, extension: Extension) -> None:
        """Install an extension and its resources after checking all declarations.

        Register dependency providers first. Duplicate identifiers, methods,
        resources, and attempts to replace base protocol methods are rejected.
        """
        from aiohttp_tiny_mcp.protocol.selection import AdapterSet

        if extension.name in self.extensions:
            raise ValueError(f"duplicate extension: {extension.name}")
        adapters = AdapterSet.default().adapters
        for name, bound in extension.methods.items():
            if (
                name.startswith("notifications/")
                or name in {"sampling/createMessage", "roots/list", "elicitation/create"}
                or any(adapter.operation_for(name) is not None for adapter in adapters)
            ):
                raise ValueError(f"extension cannot replace protocol method: {name}")
            if any(name in spec.methods for spec in self.extensions.values()):
                raise ValueError(f"duplicate extension method: {name}")
            self.check(name, bound)
        for method in extension.notifications:
            if any(adapter.operation_for(method) is not None for adapter in adapters):
                raise ValueError(f"extension cannot broadcast a protocol notification: {method}")
            if method in self.broadcasts:
                raise ValueError(f"duplicate broadcast notification: {method}")
        routes, methods = extension.method_resources()
        manifest = extension.manifest_resource(routes, methods)
        assert manifest.definition is not None
        if manifest.definition.uri in extension.resources:
            raise ValueError("duplicate extension manifest resource")
        if set(routes) & set(extension.resources):
            raise ValueError("duplicate extension method resource")
        resources = {**extension.resources, **routes, manifest.definition.uri: manifest}
        templates = {
            address
            for spec in self.resources_templated
            if spec.template is not None
            for address in (spec.template.uri_template, spec.legacy_uri)
            if address is not None
        }
        claimed: set[str] = set()
        for uri, resource in resources.items():
            for address in {uri, resource.legacy_uri}:
                if address is None:
                    continue
                if (
                    address in claimed
                    or address in self.resources_fixed
                    or address in self.resource_aliases
                    or address in templates
                ):
                    raise ValueError(f"duplicate resource: {address}")
                claimed.add(address)
            self.check(uri, resource.bound)
        self.extensions[extension.name] = extension.snapshot()
        for uri, resource in resources.items():
            resource = resource.model_copy(deep=True)
            if resource.definition is not None:
                self.resources_fixed[uri] = resource
                if resource.legacy_uri is not None:
                    self.resource_aliases[resource.legacy_uri] = resource
            else:
                if resource.legacy_only:
                    # Method query routes take precedence over file templates
                    # such as skills/{name}, whose matcher also accepts '?'.
                    self.resources_templated.insert(0, resource)
                else:
                    self.resources_templated.append(resource)

    @property
    def broadcasts(self) -> frozenset[str]:
        """Every notification method some installed extension may broadcast."""
        return frozenset(self.topic_fields)

    @property
    def topic_fields(self) -> dict[str, str | None]:
        """Each declared broadcast with the params field that carries its topic, or None."""
        found: dict[str, str | None] = {}
        for spec in self.extensions.values():
            found.update(spec.notifications)
        return found

    async def broadcast(
        self,
        method: str,
        params: Mapping[str, Any] | None = None,
        *,
        meta: Mapping[str, Any] | None = None,
    ) -> Cursor:
        """Send one notification to every client listening in the current namespace.

        `method` must be declared by an installed extension. A method declared
        with a topic field is a multicast: `params` must carry that field as a
        string, and a listener that named topics gets the event only when the
        value is one of them. The event goes through the hub, so a listener
        on another worker gets it too, and a legacy stream that reconnects
        with `Last-Event-ID` is replayed it. Returns the hub id the event was
        stored under.
        """
        fields = self.topic_fields
        if method not in fields:
            raise ValueError(f"no installed extension declares the broadcast {method!r}")
        sent: dict[str, Any] = dict(params or {})
        field = fields[method]
        if field is not None and not isinstance(sent.get(field), str):
            raise ValueError(f"{method} is a multicast: params[{field!r}] must be a string topic")
        if meta:
            sent["_meta"] = {**dict(sent.get("_meta") or {}), **meta}
        return await self.hub.publish(
            topic(NOTIFICATIONS), {"jsonrpc": "2.0", "method": method, "params": sent}
        )

    def provide(self, kind: type, source: Any) -> None:
        """Bind a type to where it comes from. Call before registering
        handlers -- plans are checked at registration, not at first call.

        `source` is an async callable taking the exchange, an async generator
        for anything that must be released afterwards, or a `web.AppKey`. For
        an object that already exists, use `provide_instance` instead.
        """
        self.providers[kind] = source

    def provide_instance(self, value: Any, kind: type | None = None) -> None:
        """Register an existing object under its type, or under an explicit `kind` such as a base
        class or protocol. Use `provide` for per-request construction or cleanup.
        """
        if kind is None and isinstance(value, type):
            raise TypeError(
                f"provide_instance was given the class {value.__name__} rather than an "
                f"instance of it. Pass the object, or say kind=type to provide the class "
                f"itself."
            )
        self.providers[kind or type(value)] = Instance(value)

    def check(self, what: str, bound: Bound) -> None:
        for name, kind in bound.plan:
            if kind is Principal:
                if self.auth is None:
                    raise TypeError(
                        f"{what}: parameter {name!r} wants a Principal, but this registry "
                        f"has no auth= -- pass an Authentication policy to verify callers first"
                    )
                continue
            if kind not in self.providers and kind is not Exchange:
                label = getattr(kind, "__name__", kind)
                raise TypeError(
                    f"{what}: parameter {name!r} wants {label}, which has no provider -- "
                    f"call registry.provide_instance() or registry.provide() first"
                )

    def tool(self, fn: Callable[..., Awaitable[Any]] | None = None, **kw: Any):
        def register(fn: Callable[..., Awaitable[Any]]):
            spec = ToolSpec.build(fn, **kw)
            if spec.scopes and self.auth is None:
                raise TypeError(
                    f"{spec.name}: scopes were declared, but this registry has no auth= -- "
                    f"a scope nothing verifies is a check that never runs"
                )
            self.check(spec.name, spec.bound)
            if spec.name in self.tools:
                raise ValueError(f"duplicate tool: {spec.name}")
            self.tools[spec.name] = spec
            return fn

        return register(fn) if fn else register

    def resource(self, uri: str, fn: Callable[..., Awaitable[Any]] | None = None, **kw: Any):
        def register(fn: Callable[..., Awaitable[Any]]):
            spec = ResourceSpec.build(uri, fn, **kw)
            self.check(uri, spec.bound)
            if spec.definition is not None:
                if (
                    spec.definition.uri in self.resources_fixed
                    or spec.definition.uri in self.resource_aliases
                ):
                    raise ValueError(f"duplicate resource: {spec.definition.uri}")
                self.resources_fixed[spec.definition.uri] = spec
            else:
                template = spec.template.uri_template if spec.template is not None else uri
                if any(
                    item.template is not None
                    and template in (item.template.uri_template, item.legacy_uri)
                    for item in self.resources_templated
                ):
                    raise ValueError(f"duplicate resource template: {template}")
                self.resources_templated.append(spec)
            return fn

        return register(fn) if fn else register

    def prompt(self, fn: Callable[..., Awaitable[Any]] | None = None, **kw: Any):
        def register(fn: Callable[..., Awaitable[Any]]):
            spec = PromptSpec.build(fn, **kw)
            self.check(spec.name, spec.bound)
            if spec.name in self.prompts:
                raise ValueError(f"duplicate prompt: {spec.name}")
            self.prompts[spec.name] = spec
            return fn

        return register(fn) if fn else register

    def completions(self, fn: Callable[..., Awaitable[Any]]):
        """(CompleteParams-shaped model, deps) -> list[str] | Completion."""
        if self.completer is not None:
            raise ValueError("duplicate completions handler")
        self.completer = Bound.of(fn)
        self.check("completions", self.completer)
        return fn

    def match_resource(self, uri: str) -> tuple[ResourceSpec, dict[str, str]] | None:
        fixed = self.resources_fixed.get(uri) or self.resource_aliases.get(uri)
        if fixed is not None:
            return fixed, {}
        for spec in self.resources_templated:
            m = spec.pattern.match(uri)
            if m is None and spec.legacy_pattern is not None:
                m = spec.legacy_pattern.match(uri)
            if m:
                return spec, m.groupdict()
        return None
