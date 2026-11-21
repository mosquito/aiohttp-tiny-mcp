from typing import Any

import pytest
from aiohttp.test_utils import BaseTestServer
from playwright.async_api import BrowserContext, Page, Request, expect

pytestmark = [pytest.mark.timeout(30), pytest.mark.asyncio(loop_scope="session")]


def requests_sent(requests: list[Request]) -> list[dict[str, Any]]:
    return [request.post_data_json or {} for request in requests if request.method == "POST"]


async def open_console(
    context: BrowserContext, server: BaseTestServer, query: str, version: str
) -> tuple[Page, list[Request]]:
    page = await context.new_page()
    requests: list[Request] = []
    page.on("request", lambda request: requests.append(request))
    await page.goto(str(server.make_url("/console")))
    await page.locator("#revision").select_option(version)
    await page.locator("#endpoint").fill("/mcp" + query)
    await page.locator("#connect").click()
    await expect(page.locator("#connect")).to_have_text("Disconnect")
    await expect(page.locator("#status")).to_have_class("status on")
    return page, requests


async def call_resource_tools(page: Page) -> None:
    await page.locator("#catalogue .item").filter(has_text="mcp_resources_list").click()
    await page.locator("#invoke").click()
    await expect(page.locator("#outcome")).to_contain_text("file://alpha")
    await expect(page.locator("#outcome")).to_contain_text("file://entries/{name}")
    await expect(page.locator("#outcome")).to_contain_text('"items"')
    await expect(page.locator("#outcome")).not_to_contain_text('"resourceTemplates"')
    await expect(page.locator("#outcome")).not_to_contain_text('"name":')
    await expect(page.locator("#outcome")).not_to_contain_text('"title":')
    await (
        page.locator("#arguments .argument")
        .filter(has=page.locator('[data-name="templates"]'))
        .locator(".null-toggle input")
        .uncheck()
    )
    await page.locator('#arguments [data-name="templates"]').check()
    await page.locator("#invoke").click()
    await expect(page.locator("#outcome")).to_contain_text("file://entries/{name}")
    await expect(page.locator("#outcome")).not_to_contain_text("file://alpha")
    await page.locator('#arguments [data-name="templates"]').uncheck()
    await page.locator("#invoke").click()
    await expect(page.locator("#outcome")).to_contain_text("file://alpha")
    await expect(page.locator("#outcome")).not_to_contain_text("file://entries/{name}")
    await page.locator("#catalogue .item").filter(has_text="mcp_resources_read").click()
    await page.locator('#arguments [data-name="uri"]').fill("file://alpha")
    await page.locator("#invoke").click()
    await expect(page.locator("#outcome")).to_contain_text("alpha content")


def assert_no_native_resources(requests: list[Request]) -> None:
    for message in requests_sent(requests):
        assert not message.get("method", "").startswith("resources/")
        if message.get("method") == "subscriptions/listen":
            selected = message["params"]["notifications"]
            assert not selected.get("resourcesListChanged")
            assert not selected.get("resourceSubscriptions")


@pytest.mark.parametrize("version", ["2025-11-25", "2026-07-28"])
@pytest.mark.parametrize(
    "query,native,tools",
    [
        ("?resources=tool", False, True),
        ("", True, False),
        ("?resources=tool&resources=native", True, True),
    ],
)
async def test_catalogue_and_refresh_follow_server_capabilities(
    context: BrowserContext,
    resource_server: BaseTestServer,
    version: str,
    query: str,
    native: bool,
    tools: bool,
) -> None:
    page, requests = await open_console(context, resource_server, query, version)
    await expect(page.locator("#catalogue .item")).to_have_count(
        (2 if native else 0) + (2 if tools else 0)
    )
    method = "resources/templates/list" if native else "tools/list"
    async with page.expect_response(
        lambda response: (
            response.request.method == "POST"
            and (response.request.post_data_json or {}).get("method") == method
        )
    ):
        await page.locator("#refresh").click()
    await expect(page.locator("#catalogue .item")).to_have_count(
        (2 if native else 0) + (2 if tools else 0)
    )
    await expect(page.locator("#status")).to_have_class("status on")
    if tools:
        await call_resource_tools(page)
    methods = [message.get("method") for message in requests_sent(requests)]
    assert "prompts/list" not in methods
    assert ("tools/list" in methods) is tools
    assert ("resources/list" in methods) is native
    assert ("resources/templates/list" in methods) is native
    if not native:
        assert_no_native_resources(requests)


@pytest.mark.parametrize("version", ["2025-11-25", "2026-07-28"])
async def test_reconnect_to_tool_mode_clears_native_catalogue_and_subscriptions(
    context: BrowserContext, resource_server: BaseTestServer, version: str
) -> None:
    page, requests = await open_console(context, resource_server, "", version)
    await expect(page.locator("#catalogue .item")).to_have_count(2)
    await page.locator("#catalogue .item").filter(has_text="file://alpha").click()
    await page.locator("#invoke").click()
    await expect(page.locator("#outcome")).to_contain_text("alpha content")
    await page.locator("#connect").click()
    await expect(page.locator("#connect")).to_have_text("Connect")
    requests.clear()
    await page.locator("#endpoint").fill("/mcp?resources=tool")
    await page.locator("#connect").click()
    await expect(page.locator("#connect")).to_have_text("Disconnect")
    await expect(page.locator("#catalogue .item")).to_have_count(2)
    await expect(page.locator("#catalogue")).not_to_contain_text("file://alpha")
    await expect(page.locator("#catalogue")).not_to_contain_text("file://entries/{name}")
    await expect(page.locator("#subject")).to_have_text("Nothing selected")
    await call_resource_tools(page)
    assert_no_native_resources(requests)
