"""Optional tool access to resources and per-request interface selection."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, quote, unquote, urlsplit

from aiohttp import web
from pydantic import BaseModel, ConfigDict, Field

from aiohttp_tiny_mcp.protocol.adapter import Adapter, RequestLike
from aiohttp_tiny_mcp.protocol.core import Failure, Outcome, Value
from aiohttp_tiny_mcp.protocol.models import (
    CallToolResult,
    Hint,
    ListParams,
    PromptArgumentDef,
    ResourceDef,
    ResourceTemplateDef,
)

from .exchange import Exchange
from .specs import ToolSpec

if TYPE_CHECKING:
    from .registry import Registry

RESOURCE_TOOL_NAMES = frozenset({"mcp_resources_list", "mcp_resources_read"})
PROMPT_SCHEME = "mcp-prompt:"
NATIVE = frozenset({"native"})


def resource_modes(registry: Registry, request: RequestLike) -> frozenset[str]:
    """Read interface modes without assigning state to a shared registry."""
    if not registry.resource_tools:
        return NATIVE
    if not isinstance(request, web.Request):
        from .stdio import StdioRequest

        return request.resource_modes if isinstance(request, StdioRequest) else NATIVE
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
        description=(
            "Omit or use null for all entries; true lists address templates, "
            "false lists ready-to-use addresses."
        ),
    )
    query: str | None = Field(
        default=None,
        description=(
            "Text to find in an address, ignoring case. "
            "Omit, use null or an empty string to list all entries."
        ),
    )
    cursor: str | None = Field(
        default=None,
        description="Pass nextCursor to get the next page; keep templates and query unchanged.",
    )


class ResourceReadArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    uri: str = Field(
        description=(
            "An address from mcp_resources_list. "
            "Replace any {placeholders} with concrete values first."
        )
    )


class PromptAddress(ResourceTemplateDef):
    arguments: list[PromptArgumentDef]


def prompts_as_resources(registry: Registry, request: RequestLike) -> bool:
    if not registry.resource_tools:
        return False
    if isinstance(request, web.Request):
        return "resource" in request.query.getall("prompts", ())

    from .stdio import StdioRequest

    return isinstance(request, StdioRequest) and request.prompts_as_resources


def prompt_addresses(registry: Registry, adapter: Adapter) -> list[PromptAddress]:
    addresses = []
    for spec in registry.prompts.values():
        prompt = adapter.describe_prompt(spec)
        if prompt is None:
            continue
        uri = f"{PROMPT_SCHEME}///{quote(prompt.name, safe='')}"
        description = prompt.description or f"Render the {prompt.name} prompt."
        if prompt.arguments:
            uri += "?arguments={arguments}"
            fields = ", ".join(
                f"{arg.name} ({'required' if arg.required else 'optional'})"
                for arg in prompt.arguments
            )
            description += f" Percent-encode the JSON arguments object. Fields: {fields}."
        addresses.append(
            PromptAddress(
                uri_template=uri,
                name=prompt.name,
                description=description,
                mime_type="application/json",
                arguments=prompt.arguments,
            )
        )
    return addresses


def prompt_address(uri: str) -> tuple[str, dict[str, Any]]:
    address = urlsplit(uri)
    name = address.path.removeprefix("/")
    # Python 3.10 rejects an empty query in strict mode.
    params = (
        parse_qs(address.query, keep_blank_values=True, strict_parsing=True, errors="strict")
        if address.query
        else {}
    )
    encoded = params.get("arguments", ["{}"])
    if (
        address.netloc
        or address.fragment
        or not address.path.startswith("/")
        or not name
        or "/" in name
        or set(params) - {"arguments"}
        or len(encoded) != 1
    ):
        raise ValueError("invalid prompt address")
    arguments = json.loads(encoded[0])
    if not isinstance(arguments, dict):
        raise ValueError("prompt arguments must be a JSON object")
    return unquote(name, errors="strict"), arguments


def resource_result(outcome: Outcome) -> CallToolResult:
    if isinstance(outcome, Failure):
        return CallToolResult.failure(outcome.message)
    assert isinstance(outcome, Value)
    return CallToolResult.of(
        outcome.result.model_dump(mode="json", by_alias=True, exclude_none=True)
    )


async def mcp_resources_list(args: ResourceListArguments, ex: Exchange) -> CallToolResult:
    """Call this tool to inspect available URL-addressed data and find addresses to read."""
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
        entries.extend(
            (f"template:{address.uri_template}", address)
            for address in prompt_addresses(ex.registry, ex.adapter)
            if query in address.uri_template.casefold()
        )
    entries.sort(key=lambda entry: entry[0])
    page = Dispatcher(ex.registry).paged(
        ex, entries, lambda entry: entry[0], ListParams(cursor=args.cursor)
    )
    if isinstance(page, Failure):
        return CallToolResult.failure(page.message)
    shown, next_cursor = page
    # A name and a title are for a person to read, and an icon is bytes or a URL for
    # one to look at. None of the three helps the model that reads this catalogue.
    result: dict[str, Any] = {
        "items": [
            definition.model_dump(
                mode="json",
                by_alias=True,
                exclude_none=True,
                exclude={"name", "title", "icons"},
            )
            for key, definition in shown
        ]
    }
    if next_cursor is not None:
        result["nextCursor"] = next_cursor
    return CallToolResult.of(result)


async def mcp_resources_read(args: ResourceReadArguments, ex: Exchange) -> CallToolResult:
    """Read URL-addressed data as text or base64-encoded binary content."""
    from .dispatcher import Dispatcher

    dispatcher = Dispatcher(ex.registry)
    if args.uri.casefold().startswith(PROMPT_SCHEME):
        return resource_result(await dispatcher.read_prompt_address(ex, args.uri))
    return resource_result(await dispatcher.read_resource(ex, uri=args.uri))


def resource_tool_specs() -> dict[str, ToolSpec]:
    return {
        "mcp_resources_list": ToolSpec.build(mcp_resources_list, annotations=Hint.READ_ONLY),
        "mcp_resources_read": ToolSpec.build(mcp_resources_read),
    }
