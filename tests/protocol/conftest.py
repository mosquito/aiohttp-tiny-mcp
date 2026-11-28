import pytest
from pydantic import BaseModel

from aiohttp_tiny_mcp import Exchange, Icon, MemoryHub, MemorySessionStore, Registry, elicit
from aiohttp_tiny_mcp.protocol.models import AudioContent, CallToolResult, ResourceLink, TextContent
from aiohttp_tiny_mcp.protocol.selection import AdapterSet


class Nothing(BaseModel):
    pass


async def careful(args: Nothing) -> str:
    """Declares hints this revision has no field for."""
    return "done"


async def speaking(args: Nothing) -> CallToolResult:
    """Returns audio, which arrived in 2025-03-26."""
    return CallToolResult(
        content=[TextContent(text="listen"), AudioContent(data="AAA=", mime_type="audio/wav")]
    )


async def asking(args: Nothing, ex: Exchange) -> str:
    """Asks, on a revision that predates elicitation by two releases."""
    agreed = await ex.ask("confirm", elicit("Go ahead?"))
    return "went ahead" if agreed.accepted else f"stopped at {agreed.action.value}"


@pytest.fixture
def adapter_registry() -> Registry:
    registry = Registry("p", "1.0", hub=MemoryHub(), session_store=MemorySessionStore())
    registry.tool(careful, annotations={"readOnlyHint": True, "destructiveHint": False})
    registry.tool(speaking)
    registry.tool(asking)
    return registry


class Sum(BaseModel):
    total: int


async def counted(args: Nothing) -> Sum:
    """Returns a model, so it has an output schema and structured content."""
    return Sum(total=5)


async def linking(args: Nothing) -> CallToolResult:
    """Returns a link, which is a kind of content 2025-06-18 introduced."""
    return CallToolResult(
        content=[TextContent(text="see this"), ResourceLink(uri="config://app", name="config")]
    )


async def described(args: Nothing) -> str:
    """Has a display title."""
    return "ok"


ICON = Icon(src="https://example.test/i.png", sizes=["48x48"], mime_type="image/png")


@pytest.fixture
def projection_registry() -> Registry:
    registry = Registry(
        "p",
        "1.0",
        hub=MemoryHub(),
        session_store=MemorySessionStore(),
        icons=[ICON],
        website_url="https://example.test",
    )
    registry.tool(counted)
    registry.tool(linking)
    registry.tool(described, title="A Display Title", icons=[ICON])
    registry.resource("config://app", counted, title="Configuration", icons=[ICON])
    registry.resource("items://{id}", counted, name="item", icons=[ICON])
    registry.prompt(described, icons=[ICON])
    return registry


@pytest.fixture
def adapters() -> AdapterSet:
    return AdapterSet.default()
