"""The rules this package applies, checked against the official SDK's own.

Agreeing with ourselves proves only that we are consistent. These tests put each rule
beside the SDK's implementation of the same rule, so a divergence is either caught or
declared here.
"""

from __future__ import annotations

from types import MappingProxyType

import pytest
from mcp import Client, ClientSession
from mcp.shared.tool_name_validation import validate_tool_name
from mcp.shared.uri_template import InvalidUriTemplate, UriTemplate
from mcp_types import ElicitResult
from pydantic import BaseModel

from aiohttp_tiny_mcp import CacheHint, MemoryHub, MemorySessionStore, Registry
from aiohttp_tiny_mcp.server.naming import checked_tool_name, template_variables
from aiohttp_tiny_mcp.testing import serving

# `asyncio_mode = auto` runs the async tests here, and the rest are plain functions.

NAMES = (
    "add",
    "a.b-c",
    "A",
    "0",
    "mcp_resources_list",
    "a" * 128,
    "",
    "a" * 129,
    "my tool",
    "a,b",
    "search/items",
    "tool:read",
    "Stra\u00dfe",
)

SERVED_URIS = (
    "config://app",
    "db://users/{id}",
    "files://{path}",
    "chat://channels/{channel}/messages/{after}",
    "skill://{name}/SKILL.md",
    "mcp-extensions://example/echo?params={params}",
    "file://Stra\u00dfe/{name}",
    "x://{a}-{b}",
)

STRUCTURAL_FAULTS = (
    "notebook://entries/{id",
    "x://a/{id}/b/{id}",
    "x://{id}{path}",
    "x://{}",
)

#: The SDK accepts these and this package does not, on purpose. An operator is not
#: implemented here, and a variable becomes a field of a Pydantic model, so it is a
#: Python name.
NARROWER = ("x://{+path}", "x://{#frag}", "x://{1id}")


def test_the_tool_name_rule_agrees_with_the_sdk():
    """One regex, two implementations. A name is refused here exactly where the SDK
    calls it invalid.
    """
    for name in NAMES:
        valid = validate_tool_name(name).is_valid
        try:
            checked_tool_name(name)
            refused = False
        except ValueError:
            refused = True
        assert refused is not valid, name


def test_a_discouraged_name_registers_in_both():
    """A leading dash is a warning in the specification, so neither implementation
    refuses it.
    """
    assert validate_tool_name("-lead").is_valid
    assert validate_tool_name("-lead").warnings
    assert checked_tool_name("-lead") == "-lead"


@pytest.mark.parametrize("uri", SERVED_URIS)
def test_every_uri_this_package_serves_also_parses_in_the_sdk(uri):
    """This package's syntax is a subset, so a host built on the SDK can address it."""
    template_variables(uri)
    UriTemplate.parse(uri)


@pytest.mark.parametrize("uri", STRUCTURAL_FAULTS)
def test_a_structural_fault_is_refused_by_both(uri):
    """Where both implement the rule, both refuse: this is not a local opinion."""
    with pytest.raises(ValueError):
        template_variables(uri)
    with pytest.raises(InvalidUriTemplate):
        UriTemplate.parse(uri)


@pytest.mark.parametrize("uri", NARROWER)
def test_a_declared_divergence_stays_declared(uri):
    """The SDK implements more of RFC 6570. A change there must fail here, not pass
    silently.
    """
    UriTemplate.parse(uri)
    with pytest.raises(ValueError):
        template_variables(uri)


class Nothing(BaseModel):
    pass


class Ref(BaseModel):
    id: int


async def anything(args: Nothing) -> str:
    """Stands in for a handler."""
    return "ok"


async def item(args: Ref) -> str:
    """One item by id."""
    return f"item-{args.id}"


HINTS = MappingProxyType(
    {
        "tools/list": CacheHint(cache_ttl=30, scope="public"),
        "prompts/list": CacheHint(cache_ttl=20),
        "resources/list": CacheHint(cache_ttl=10, scope="public"),
        "resources/templates/list": CacheHint(cache_ttl=15, scope="public"),
        "resources/read": CacheHint(cache_ttl=1, scope="public"),
        "server/discover": CacheHint(cache_ttl=5, scope="public"),
    }
)


@pytest.fixture
async def cached_server():
    registry = Registry(
        "cached",
        "1.0",
        hub=MemoryHub(),
        session_store=MemorySessionStore(),
        cache_hints=HINTS,
    )
    registry.tool(anything)
    registry.prompt(anything)
    registry.resource("config://app", anything)
    registry.resource("items://{id}", item, name="item")
    async with serving(registry) as url:
        yield url


async def test_the_sdk_client_reads_the_freshness_of_every_cacheable_result(cached_server):
    """The SDK's models require both fields on a cacheable result, so parsing is the
    check. A missing `cacheScope` would be a validation error there, not a silent gap.
    """
    async with Client(cached_server, mode="2026-07-28") as client:
        tools = await client.list_tools()
        assert (tools.ttl_ms, tools.cache_scope) == (30_000, "public")

        prompts = await client.list_prompts()
        assert (prompts.ttl_ms, prompts.cache_scope) == (20_000, "private")

        resources = await client.list_resources()
        assert (resources.ttl_ms, resources.cache_scope) == (10_000, "public")

        templates = await client.list_resource_templates()
        assert (templates.ttl_ms, templates.cache_scope) == (15_000, "public")

        read = await client.read_resource("items://42")
        assert (read.ttl_ms, read.cache_scope) == (1_000, "public")


async def test_the_official_sse_client_answers_a_pushed_question(offered_over_sse):
    """The SDK's own client for the 2024-11-05 transport, asked something mid-call.

    The question goes down the stream it opened, and the answer arrives on a separate
    request, which is the half this package did not serve.
    """
    from mcp.client.sse import sse_client

    async def elicitation_callback(context, params):
        return ElicitResult(action="accept", content={})

    async with sse_client(f"{offered_over_sse}/sse") as (read, write):
        async with ClientSession(read, write, elicitation_callback=elicitation_callback) as session:
            await session.initialize()
            answered = await session.call_tool("gate", {})

    assert answered.content[0].text == "accepted"
