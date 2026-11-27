from __future__ import annotations

import re
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from aiohttp.test_utils import BaseTestServer, TestServer
from playwright.async_api import BrowserContext, Page, expect

from aiohttp_tiny_mcp import Endpoint, Registry
from aiohttp_tiny_mcp.console import Console
from aiohttp_tiny_mcp.protocol.selection import AdapterSet
from aiohttp_tiny_mcp.storage.hub import NOTIFICATIONS, topic
from examples.demo import deploy, summarize

pytestmark = [pytest.mark.timeout(30), pytest.mark.asyncio(loop_scope="session")]
VERSIONS = AdapterSet.default().versions


@pytest_asyncio.fixture(loop_scope="session")
async def sampling_server(
    context: BrowserContext,
) -> AsyncIterator[tuple[BaseTestServer, Registry]]:
    registry = Registry("sampling-console", "1.0")
    registry.tool(summarize)
    registry.tool(deploy)
    app = Endpoint(registry).app()
    Console().setup(app)
    async with TestServer(app) as server:
        try:
            yield server, registry
        finally:
            await context.close()


async def open_sampling(
    context: BrowserContext, server: BaseTestServer, version: str, *, enabled: bool = True
) -> Page:
    page = await context.new_page()
    await page.goto(str(server.make_url("/console")))
    await page.locator("#revision").select_option(version)
    await page.get_by_label("Can be asked", exact=True).uncheck()
    await page.get_by_label("Can answer for a model").set_checked(enabled)
    async with page.expect_response(
        lambda response: (
            response.url.endswith("/mcp")
            and (
                response.request.method == "GET"
                or (response.request.post_data_json or {}).get("method") == "subscriptions/listen"
            )
        )
    ):
        await page.locator("#connect").click()
    await expect(page.locator("#connect")).to_have_text("Disconnect")
    await page.get_by_role("button", name=re.compile(r"^summarize")).click()
    await page.locator('#arguments [data-name="text"]').fill("Trees store carbon.")
    await page.locator("#invoke").click()
    return page


@pytest.mark.parametrize("version", VERSIONS)
async def test_a_person_answers_the_example_sampling_tool(
    context: BrowserContext, sampling_server: tuple[BaseTestServer, Registry], version: str
) -> None:
    server, _ = sampling_server
    page = await open_sampling(context, server, version)
    dialog = page.get_by_role("dialog", name="Answer for a model")
    await expect(dialog).to_be_visible()
    details = dialog.locator("#sampling-details")
    await expect(details.locator(".value-tree")).to_be_visible()
    await expect(details.locator(".value-tree")).to_contain_text("Trees store carbon.")
    await expect(details.locator(".value-raw")).to_be_hidden()
    await details.get_by_role("button", name="raw", exact=True).click()
    await expect(details.locator(".value-tree")).to_be_hidden()
    await expect(details.locator(".value-raw")).to_be_visible()
    for detail in (
        "Trees store carbon.",
        "Summarize the text in one sentence.",
        '"maxTokens": 128',
        '"modelPreferences"',
        '"includeContext": "none"',
        '"temperature": 0.2',
    ):
        await expect(dialog).to_contain_text(detail)
    await details.get_by_role("button", name="tree", exact=True).click()
    await expect(details.locator(".value-tree")).to_be_visible()
    await expect(dialog).to_contain_text("The server is waiting")
    await dialog.get_by_label("Completion").fill("Trees help the climate.\nKeep them.")
    await dialog.get_by_role("button", name="Send completion").click()
    await expect(dialog).not_to_be_visible()
    await expect(page.locator("#outcome")).to_contain_text(
        "console-operator: Trees help the climate.\nKeep them."
    )
    await page.locator("#invoke").click()
    await expect(dialog).to_be_visible()
    await expect(dialog.get_by_label("Completion")).to_have_value("")
    await page.keyboard.press("Escape")
    await expect(page.locator("#outcome")).to_contain_text("Completion was refused")


@pytest.mark.parametrize("version", VERSIONS)
async def test_sampling_is_refused_without_the_checkbox(
    context: BrowserContext, sampling_server: tuple[BaseTestServer, Registry], version: str
) -> None:
    server, _ = sampling_server
    page = await open_sampling(context, server, version, enabled=False)
    await expect(page.locator("#outcome")).to_contain_text(
        "client did not declare the sampling capability"
    )
    await expect(page.locator("#sampling-request")).not_to_be_visible()


@pytest.mark.parametrize("version", ["2026-07-28", "2025-11-25"])
@pytest.mark.parametrize("action", ["Decline", "Cancel", "Escape"])
async def test_refusing_a_completion_is_a_protocol_error(
    context: BrowserContext,
    sampling_server: tuple[BaseTestServer, Registry],
    version: str,
    action: str,
) -> None:
    server, _ = sampling_server
    page = await open_sampling(context, server, version)
    replies = []
    page.on(
        "request",
        lambda request: (
            replies.append(request.post_data_json or {}) if request.method == "POST" else None
        ),
    )
    dialog = page.get_by_role("dialog", name="Answer for a model")
    await expect(dialog).to_be_visible()
    if action == "Escape":
        await page.keyboard.press("Escape")
    else:
        await dialog.get_by_role("button", name=action, exact=True).click()
    await expect(page.locator("#outcome")).to_contain_text("Completion was refused")
    await expect(dialog).not_to_be_visible()
    if version == "2026-07-28":
        assert any(
            reply.get("params", {}).get("inputResponses", {}).get("completion", {}).get("code")
            == -32000
            for reply in replies
        )
    else:
        assert any(reply.get("error", {}).get("code") == -32000 for reply in replies)


@pytest.mark.parametrize("version", ["2026-07-28", "2025-11-25"])
async def test_notifications_continue_while_a_completion_is_pending(
    context: BrowserContext, sampling_server: tuple[BaseTestServer, Registry], version: str
) -> None:
    server, registry = sampling_server
    page = await open_sampling(context, server, version)
    dialog = page.get_by_role("dialog", name="Answer for a model")
    await expect(dialog).to_be_visible()
    await registry.hub.publish(
        topic(NOTIFICATIONS),
        {"jsonrpc": "2.0", "method": "notifications/tools/list_changed", "params": {}},
    )
    await expect(page.locator("#traffic")).to_contain_text("notifications/tools/list_changed")
    await expect(dialog).to_be_visible()
    await dialog.get_by_role("button", name="Cancel", exact=True).click()


@pytest.mark.parametrize("version", ["2026-07-28", "2025-11-25"])
async def test_a_question_still_reaches_the_person(
    context: BrowserContext, sampling_server: tuple[BaseTestServer, Registry], version: str
) -> None:
    server, _ = sampling_server
    page = await context.new_page()
    await page.goto(str(server.make_url("/console")))
    await page.locator("#revision").select_option(version)
    await page.locator("#connect").click()
    await expect(page.locator("#connect")).to_have_text("Disconnect")
    await page.get_by_role("button", name=re.compile(r"^deploy")).click()
    await page.locator('#arguments [data-name="service"]').fill("example")
    await page.locator("#invoke").click()
    await expect(page.locator("#question")).to_be_visible()
    await page.locator("#question").get_by_role("button", name="Accept", exact=True).click()
    await expect(page.locator("#outcome")).to_contain_text("deployed example")
    await expect(page.locator("#sampling-request")).not_to_be_visible()
