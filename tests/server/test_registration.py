"""Register handlers directly, including bound methods."""

from __future__ import annotations

import pytest
from pydantic import BaseModel, ValidationError

from aiohttp_tiny_mcp import Client, Icon, MemoryHub, MemorySessionStore, Registry
from aiohttp_tiny_mcp.protocol.selection import AdapterSet

pytestmark = pytest.mark.asyncio

MODERN = AdapterSet.default().by_version["2026-07-28"]


class Add(BaseModel):
    a: int
    b: int


class Nothing(BaseModel):
    pass


class Ref(BaseModel):
    id: int


class Lang(BaseModel):
    language: str


async def add(args: Add) -> int:
    """Add two integers."""
    return args.a + args.b


async def config(args: Nothing) -> dict:
    """Application configuration."""
    return {"debug": False}


async def item(args: Ref) -> str:
    """One item by id."""
    return f"item-{args.id}"


async def greet(args: Lang) -> str:
    """Ask for a greeting."""
    return f"Hello, {args.language} speaker."


def fresh() -> Registry:
    return Registry("plain", "1.0", hub=MemoryHub(), session_store=MemorySessionStore())


async def test_everything_registers_by_argument(real_endpoint):
    registry = fresh()
    registry.tool(add)
    registry.resource("config://app", config, mime_type="application/json")
    registry.resource("catalog://items/{id}", item, name="item")
    registry.prompt(greet)

    async with Client(await real_endpoint(registry), MODERN) as client:
        await client.initialize()
        assert {tool.name for tool in await client.list_tools()} == {"add"}
        assert {r.uri for r in await client.list_resources()} == {"config://app"}
        assert {p.name for p in await client.list_prompts()} == {"greet"}
        assert (await client.call_tool("add", {"a": 2, "b": 3})).content[0].text == "5"
        read = await client.read_resource("catalog://items/7")
        assert "item-7" in read.contents[0].text


async def test_a_name_may_be_given_where_the_function_name_will_not_do():
    registry = fresh()
    registry.tool(add, name="sum", title="Add up")
    assert set(registry.tools) == {"sum"}
    assert registry.tools["sum"].title == "Add up"


async def test_the_decorator_form_still_works():
    registry = fresh()

    @registry.tool
    async def one(args: Nothing) -> str:
        """One."""
        return "1"

    @registry.resource("config://app")
    async def two(args: Nothing) -> dict:
        """Two."""
        return {}

    @registry.prompt
    async def three(args: Lang) -> str:
        """Three."""
        return "3"

    assert set(registry.tools) == {"one"}
    assert set(registry.resources_fixed) == {"config://app"}
    assert set(registry.prompts) == {"three"}


async def test_a_handler_may_be_a_method_that_holds_its_own_state(real_endpoint):

    class Counter:
        def __init__(self, start: int) -> None:
            self.total = start

        async def bump(self, args: Nothing) -> int:
            """Increase and report."""
            self.total += 1
            return self.total

    counter = Counter(41)
    registry = fresh()
    registry.tool(counter.bump, name="bump")

    async with Client(await real_endpoint(registry), MODERN) as client:
        await client.initialize()
        assert (await client.call_tool("bump", {})).content[0].text == "42"
    assert counter.total == 42


async def test_the_same_handler_may_be_registered_twice_under_two_names():
    registry = fresh()
    registry.tool(add, name="add")
    registry.tool(add, name="plus")
    assert set(registry.tools) == {"add", "plus"}


async def test_a_duplicate_is_still_refused():
    registry = fresh()
    registry.tool(add)
    with pytest.raises(ValueError, match="duplicate tool"):
        registry.tool(add)

    registry.resource("config://app", config)
    with pytest.raises(ValueError, match="duplicate resource"):
        registry.resource("config://app", config)


@pytest.mark.parametrize(
    ("uri", "fault"),
    [
        ("notebook://entries/{id", "is never closed"),
        ("x://a}b", "closes nothing"),
        ("x://{1id}", "is not a variable name"),
        ("x://{#frag}", "RFC 6570 operator"),
        ("x://{}", "is not a variable name"),
        ("", "must not be empty"),
        ("x://a/{id}/b/{id}", "appears more than once"),
        ("x://{id}{path}", "no literal text between them"),
    ],
)
async def test_a_malformed_resource_uri_is_refused_at_registration(uri, fault):
    """Each of these used to register, at an address no request could reach.

    A typo in a template produced a fixed resource at the literal URI, and a repeated
    variable reached the regular expression engine, which raised its own error.
    """
    registry = fresh()
    with pytest.raises(ValueError, match=fault):
        registry.resource(uri, item)
    assert not registry.resources_fixed
    assert not registry.resources_templated


@pytest.mark.parametrize(
    "uri",
    [
        "config://app",
        "file://docs/{name}",
        "file://docs/{+path}",
        "x://{a}-{b}",
        "file://Stra\u00dfe/{name}",
        "mcp-extensions://ext/method?params={params}",
    ],
)
async def test_a_usable_resource_uri_still_registers(uri):
    """The forms this package already serves keep working, templated or fixed."""
    registry = fresh()
    registry.resource(uri, item)
    assert len(registry.resources_fixed) + len(registry.resources_templated) == 1


async def test_a_tool_name_outside_the_specification_is_refused():
    """A host is allowed to reject the name, so the registry rejects it first."""
    registry = fresh()
    with pytest.raises(ValueError, match="invalid tool name"):
        registry.tool(add, name="my tool")
    with pytest.raises(ValueError, match="invalid tool name"):
        registry.tool(add, name="search/items")
    assert not registry.tools


async def test_a_reserved_name_is_still_refused_as_reserved():
    """`mcp_resources_list` is a valid name, so the reserved check is what must catch it."""
    registry = fresh()
    with pytest.raises(ValueError, match="reserved"):
        registry.tool(add, name="mcp_resources_list")


async def test_a_prompt_name_is_not_constrained():
    """The specification states a rule for tool names only."""
    registry = fresh()
    registry.prompt(greet, name="greet/user")
    assert "greet/user" in registry.prompts


async def test_a_declared_icon_reaches_the_declaration():
    registry = fresh()
    icon = Icon(src="https://example.test/i.png", sizes=["48x48", "any"])
    registry.tool(add, icons=[icon])
    registry.resource("config://app", config, icons=[icon])
    registry.prompt(greet, icons=[icon])

    assert registry.tools["add"].icons == (icon,)
    assert registry.resources_fixed["config://app"].definition.icons == (icon,)
    assert registry.prompts["greet"].definition.icons == (icon,)


@pytest.mark.parametrize(
    ("icon", "fault"),
    [
        ({"sizes": ["48x48"]}, "src"),
        ({"src": "javascript:alert(1)"}, "http:, https: or data:"),
        ({"src": "file:///etc/passwd"}, "http:, https: or data:"),
        ({"src": "https://example.test/i.png", "sizes": ["48"]}, "'any' or 'WxH'"),
        ({"src": "https://example.test/i.png", "theme": "neon"}, "theme"),
    ],
)
async def test_a_malformed_icon_is_refused_at_registration(icon, fault):
    """A declaration is checked once, here, not on every listing it reaches."""
    registry = fresh()
    with pytest.raises(ValidationError, match=fault):
        registry.tool(add, icons=[icon])
    assert not registry.tools


async def test_the_server_icons_and_website_reach_the_server_info():
    registry = Registry(
        "plain",
        "1.0",
        hub=MemoryHub(),
        session_store=MemorySessionStore(),
        icons=[Icon(src="https://example.test/i.png")],
        website_url="https://example.test",
    )
    assert registry.info.icons[0].src == "https://example.test/i.png"
    assert registry.info.website_url == "https://example.test"
