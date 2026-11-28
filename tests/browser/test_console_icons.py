from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from aiohttp.test_utils import BaseTestServer, TestServer
from playwright.async_api import BrowserContext, expect
from pydantic import BaseModel

from aiohttp_tiny_mcp import Endpoint, Icon, Icons, Registry
from aiohttp_tiny_mcp.console import Console
from aiohttp_tiny_mcp.protocol.selection import AdapterSet

pytestmark = [pytest.mark.timeout(30), pytest.mark.asyncio(loop_scope="session")]


LIGHT, DARK = [icon.src for icon in Icons.MAGNIFYING_GLASS.value]


class Nothing(BaseModel):
    pass


class Entry(BaseModel):
    id: str


@pytest_asyncio.fixture(loop_scope="session")
async def icon_server(context: BrowserContext) -> AsyncIterator[BaseTestServer]:
    icons = [Icons.MAGNIFYING_GLASS]
    registry = Registry("icons", "1.0", icons=icons, instructions="Server instructions.")

    @registry.tool(icons=icons)
    async def status(args: Nothing) -> str:
        return "ready"

    registry.tool(status, name="plain")
    registry.tool(status, name="broken", icons=[Icon(src="https://example.test/missing.svg")])
    registry.resource("status://service", status, icons=icons)
    registry.prompt(status, icons=icons)

    @registry.resource("entries://{id}", icons=icons)
    async def entry(args: Entry) -> str:
        return args.id

    app = Endpoint(registry).app()
    Console().setup(app)
    async with TestServer(app) as server:
        try:
            yield server
        finally:
            await context.close()


@pytest.mark.parametrize("version", AdapterSet.default().versions)
async def test_icons_follow_the_revision_and_theme(
    context: BrowserContext, icon_server: BaseTestServer, version: str
) -> None:
    await context.route("https://example.test/**", lambda route: route.abort())
    page = await context.new_page()
    await page.emulate_media(color_scheme="light")
    await page.goto(str(icon_server.make_url("/console")))
    await page.locator("#revision").select_option(version)
    await page.locator("#connect").click()
    await expect(page.locator("#connect")).to_have_text("Disconnect")
    icons = page.locator(".declaration-icon img")
    if version in ("2026-07-28", "2025-11-25"):
        await expect(icons).to_have_count(5)
        for icon in await icons.all():
            await expect(icon).to_be_visible()
            await expect(icon).to_have_js_property("currentSrc", LIGHT)
            assert await icon.evaluate("image => image.naturalWidth") == 128
            assert await icon.evaluate("image => image.clientWidth") == 16
        await page.emulate_media(color_scheme="dark")
        for icon in await icons.all():
            await expect(icon).to_have_js_property("currentSrc", DARK)
        await page.locator("#server-title").click()
        await expect(page.locator("#about")).to_contain_text("Server instructions.")
        await page.locator("#refresh").click()
        await expect(page.locator("#server-icon img")).to_be_visible()
    else:
        await expect(icons).to_have_count(0)
    await expect(page.get_by_role("button", name="plain", exact=True)).to_be_visible()
    await expect(page.get_by_role("button", name="broken", exact=True)).to_be_visible()
    await expect(
        page.get_by_role("button", name="broken", exact=True).locator("img")
    ).to_have_count(0)
    await page.locator("#connect").click()
    await expect(page.locator("#server-icon img")).to_have_count(0)


async def test_untrusted_icon_sources_are_not_rendered(
    context: BrowserContext, icon_server: BaseTestServer
) -> None:
    page = await context.new_page()
    await page.goto(str(icon_server.make_url("/console")))
    for source in ("javascript:alert(1)", "file:///private/icon.png", "data:text/html,<h1>x</h1>"):
        assert await page.evaluate("src => iconView([{src}]) === null", source)
