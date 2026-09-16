"""Public API: declare tools, resources and prompts once, revision-free."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from .auth import Authorization, Principal
from .exchange import Exchange, Instance
from .hub import Hub, MemoryHub
from .models import Implementation
from .request_state import DEFAULT_TTL_SECONDS as STATE_TTL_SECONDS
from .request_state import RequestStates
from .sessions import DEFAULT_TTL_SECONDS, MemorySessionStore, SessionStore
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
        auth: Authorization | None = None,
        instructions: str | None = None,
        session_ttl_seconds: int = DEFAULT_TTL_SECONDS,
        request_state_ttl_seconds: int = STATE_TTL_SECONDS,
        hub_poll_seconds: float = 30.0,
        ask_timeout_seconds: float = 120.0,
    ) -> None:
        self.info = Implementation(name=name, version=version)
        self.instructions = instructions
        self.hub = hub if hub is not None else MemoryHub()
        self.session_store = session_store if session_store is not None else MemorySessionStore()
        self.auth = auth
        self.session_ttl_seconds = session_ttl_seconds
        self.request_state = RequestStates(self.session_store, request_state_ttl_seconds)
        self.hub_poll_seconds = hub_poll_seconds
        self.ask_timeout_seconds = ask_timeout_seconds
        self.tools: dict[str, ToolSpec] = {}
        self.resources_fixed: dict[str, ResourceSpec] = {}
        self.resources_templated: list[ResourceSpec] = []
        self.prompts: dict[str, PromptSpec] = {}
        self.completer: Bound | None = None
        self.providers: dict[type, Any] = {}

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
                        f"has no auth= -- pass an Authorization to verify tokens first"
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
                if spec.definition.uri in self.resources_fixed:
                    raise ValueError(f"duplicate resource: {spec.definition.uri}")
                self.resources_fixed[spec.definition.uri] = spec
            else:
                template = spec.template.uri_template if spec.template is not None else uri
                if any(
                    item.template is not None and item.template.uri_template == template
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
        fixed = self.resources_fixed.get(uri)
        if fixed is not None:
            return fixed, {}
        for spec in self.resources_templated:
            m = spec.pattern.match(uri)
            if m:
                return spec, m.groupdict()
        return None
