"""Optional tool access to resources and per-request interface selection."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from aiohttp import web
from pydantic import BaseModel, ConfigDict, Field

from aiohttp_tiny_mcp.protocol.core import Failure, Outcome, Value
from aiohttp_tiny_mcp.protocol.models import (
    CallToolResult,
    Hint,
    ListParams,
    ResourceDef,
    ResourceTemplateDef,
)

from .exchange import Exchange
from .specs import ToolSpec

if TYPE_CHECKING:
    from aiohttp_tiny_mcp.protocol.adapter import Adapter, RequestLike

    from .registry import Registry

RESOURCE_TOOL_NAMES = frozenset({"mcp_resources_list", "mcp_resources_read"})
NATIVE = frozenset({"native"})


def resource_modes(registry: Registry, request: RequestLike) -> frozenset[str]:
    """Read interface modes without assigning state to a shared registry."""
    if not registry.resource_tools or not isinstance(request, web.Request):
        return NATIVE
    modes = frozenset(request.query.getall("resources", ["native"]))
    if not modes <= {"native", "tool"}:
        raise web.HTTPBadRequest(text="resources must be native or tool")
    return modes


def resource_capabilities(
    registry: Registry, adapter: Adapter, modes: frozenset[str]
) -> dict[str, Any]:
    caps = dict(adapter.capabilities(registry))
    if "native" not in modes:
        caps.pop("resources", None)
    if "tool" in modes and registry.resource_tool_specs:
        caps["tools"] = {"listChanged": True}
    return caps


class ResourceListArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    templates: bool | None = Field(
        default=None,
        description="Omit or use null for both; true lists templates, false lists fixed resources.",
    )
    query: str | None = Field(
        default=None,
        description=(
            "Literal, case-insensitive substring of a URI or URI template. "
            "Omit, use null or an empty string for all entries."
        ),
    )
    cursor: str | None = None


class ResourceReadArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    uri: str


def resource_result(outcome: Outcome) -> CallToolResult:
    if isinstance(outcome, Failure):
        return CallToolResult.failure(outcome.message)
    assert isinstance(outcome, Value)
    return CallToolResult.of(
        outcome.result.model_dump(mode="json", by_alias=True, exclude_none=True)
    )


async def mcp_resources_list(args: ResourceListArguments, ex: Exchange) -> CallToolResult:
    """List resources and URI templates in items. Keep filters when using nextCursor as cursor."""
    from .dispatcher import Dispatcher

    entries: list[tuple[str, ResourceDef | ResourceTemplateDef]] = []
    query = (args.query or "").casefold()
    if args.templates is not True:
        entries.extend(
            (f"resource:{definition.uri}", definition)
            for spec in ex.registry.resources_fixed.values()
            if isinstance(definition := ex.adapter.describe_resource(spec), ResourceDef)
            and query in definition.uri.casefold()
        )
    if args.templates is not False:
        entries.extend(
            (f"template:{definition.uri_template}", definition)
            for spec in ex.registry.resources_templated
            if isinstance(definition := ex.adapter.describe_resource(spec), ResourceTemplateDef)
            and query in definition.uri_template.casefold()
        )
    entries.sort(key=lambda entry: entry[0])
    page = Dispatcher(ex.registry).paged(
        ex, entries, lambda entry: entry[0], ListParams(cursor=args.cursor)
    )
    if isinstance(page, Failure):
        return CallToolResult.failure(page.message)
    shown, next_cursor = page
    result: dict[str, Any] = {
        "items": [
            definition.model_dump(
                mode="json", by_alias=True, exclude_none=True, exclude={"name", "title"}
            )
            for key, definition in shown
        ]
    }
    if next_cursor is not None:
        result["nextCursor"] = next_cursor
    return CallToolResult.of(result)


async def mcp_resources_read(args: ResourceReadArguments, ex: Exchange) -> CallToolResult:
    """Read a concrete resource URI. Return contents as JSON text, including base64 for blobs."""
    from .dispatcher import Dispatcher

    return resource_result(await Dispatcher(ex.registry).read_resource(ex, uri=args.uri))


def resource_tool_specs() -> dict[str, ToolSpec]:
    return {
        "mcp_resources_list": ToolSpec.build(mcp_resources_list, annotations=Hint.READ_ONLY),
        "mcp_resources_read": ToolSpec.build(mcp_resources_read),
    }
