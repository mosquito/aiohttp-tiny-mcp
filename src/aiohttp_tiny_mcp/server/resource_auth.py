"""Resource visibility and authorization shared by calls and notification streams."""

from __future__ import annotations

from aiohttp_tiny_mcp.auth import Principal
from aiohttp_tiny_mcp.protocol.adapter import Adapter, RegistryProtocol
from aiohttp_tiny_mcp.protocol.models import ResourceDef, ResourceTemplateDef

from .specs import ResourceSpec


def visible_resource(
    registry: RegistryProtocol, adapter: Adapter, uri: str, *, template: bool = False
) -> ResourceSpec | None:
    """Resolve a resource using only the URI spelling exposed by this revision."""
    if template:
        for spec in registry.resources_templated:
            if spec.template is None or uri not in (spec.template.uri_template, spec.legacy_uri):
                continue
            definition = adapter.describe_resource(spec)
            if isinstance(definition, ResourceTemplateDef) and definition.uri_template == uri:
                return spec
            return None
    found = registry.match_resource(uri)
    if found is None:
        return None
    spec = found[0]
    definition = adapter.describe_resource(spec)
    if definition is None:
        return None
    if isinstance(definition, ResourceDef):
        return spec if definition.uri == uri else None
    pattern = spec.pattern
    if spec.legacy_uri is not None and not adapter.supports_extensions:
        pattern = spec.legacy_pattern
    return spec if pattern is not None and pattern.fullmatch(uri) is not None else None


def resource_refusal(spec: ResourceSpec, principal: Principal | None) -> str | None:
    """Describe a resource scope failure without invoking its handler or dependencies."""
    if not spec.scopes:
        return None
    label = "resource"
    if spec.definition is not None:
        label = spec.definition.uri
    elif spec.template is not None:
        label = spec.template.uri_template
    if principal is None:
        return (
            f"{label} requires the scopes {', '.join(sorted(spec.scopes))}, and this "
            f"request carried no verified caller"
        )
    missing = principal.holds(spec.scopes)
    if missing:
        return f"{label} requires the scope {', '.join(sorted(missing))}"
    return None


def allowed_resource(
    registry: RegistryProtocol, adapter: Adapter, uri: str, principal: Principal | None
) -> bool:
    """Check a concrete resource URI before including it in a notification stream."""
    spec = visible_resource(registry, adapter, uri)
    return spec is not None and resource_refusal(spec, principal) is None
