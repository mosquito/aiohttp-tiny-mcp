from __future__ import annotations

from collections.abc import Callable

import pytest
from aiohttp import web
from aiohttp.test_utils import BaseTestServer, TestServer
from playwright.async_api import BrowserContext, Request, expect

pytestmark = [pytest.mark.timeout(60), pytest.mark.asyncio(loop_scope="session")]


async def test_reload_restores_connection_and_options_but_new_tab_stays_disconnected(
    context: BrowserContext, server: BaseTestServer
) -> None:
    page = await context.new_page()
    requests: list[Request] = []
    page.on("request", lambda request: requests.append(request))
    console_url = str(server.make_url("/console"))
    await page.goto(console_url)
    await expect(page.locator("#connect")).to_have_text("Connect")
    assert not [request for request in requests if request.method == "POST"]
    endpoint = "/mcp?org=reload.example&user=reader&extra=kept"
    await page.locator("#endpoint").fill(endpoint)
    await page.locator("#revision").select_option("2025-11-25")
    await page.locator("#answerable").uncheck()
    await page.locator("#notifications").uncheck()
    await page.locator("#connect").click()
    await expect(page.locator("#connect")).to_have_text("Disconnect")
    requests.clear()
    await page.reload()
    await expect(page.locator("#connect")).to_have_text("Disconnect")
    await expect(page.locator("#endpoint")).to_have_value(endpoint)
    await expect(page.locator("#revision")).to_have_value("2025-11-25")
    await expect(page.locator("#answerable")).not_to_be_checked()
    await expect(page.locator("#notifications")).not_to_be_checked()
    initialize = [
        request
        for request in requests
        if request.method == "POST" and (request.post_data_json or {}).get("method") == "initialize"
    ]
    assert len(initialize) == 1
    assert "mcp-session-id" not in initialize[0].headers
    assert all(request.url.endswith(endpoint) for request in requests if request.method == "POST")
    other = await page.context.new_page()
    other_requests: list[Request] = []
    other.on("request", lambda request: other_requests.append(request))
    await other.goto(console_url)
    await expect(other.locator("#connect")).to_have_text("Connect")
    await expect(other.locator("#endpoint")).to_have_value(endpoint)
    assert not [request for request in other_requests if request.method == "POST"]
    await other.close()
    await page.locator("#connect").click()
    requests.clear()
    await page.reload()
    await expect(page.locator("#connect")).to_have_text("Connect")
    await expect(page.locator("#endpoint")).to_have_value(endpoint)
    assert not [request for request in requests if request.method == "POST"]


@pytest.mark.parametrize("method", ["bearer", "query"])
async def test_reload_reuses_saved_authentication_and_clear_stops_reconnecting(
    context: BrowserContext, console_app: Callable[..., web.Application], method: str
) -> None:
    token = "console-reconnect-test-token"
    async with TestServer(console_app(token)) as running:
        page = await context.new_page()
        try:
            await page.goto(str(running.make_url("/console")))
            await page.locator("#authenticate").click()
            await page.locator("#auth-method").select_option(method)
            if method == "bearer":
                await page.locator("#auth-secret").fill(token)
            else:
                await page.locator("#endpoint-parameters .null-toggle input").uncheck()
                await page.locator('#endpoint-parameters [data-name="token"]').fill(token)
            await page.locator('#auth-form button[type="submit"]').click()
            await expect(page.locator("#connect")).to_have_text("Disconnect")
            endpoint = await page.locator("#endpoint").input_value()
            requests: list[Request] = []
            page.on("request", lambda request: requests.append(request))
            await page.reload()
            await expect(page.locator("#connect")).to_have_text("Disconnect")
            await expect(page.locator("#authentication")).not_to_be_visible()
            await expect(page.locator("#endpoint")).to_have_value(endpoint)
            sent = [request for request in requests if request.method == "POST"]
            assert sent
            assert all(request.url.endswith(endpoint) for request in sent)
            authorization = f"Bearer {token}" if method == "bearer" else None
            assert all(request.headers.get("authorization") == authorization for request in sent)
            await page.locator("#authenticate").click()
            await page.locator("#auth-clear").click()
            requests.clear()
            await page.reload()
            await expect(page.locator("#connect")).to_have_text("Connect")
            assert not [request for request in requests if request.method == "POST"]
        finally:
            await page.close()


async def test_failed_reconnect_does_not_retry_after_another_reload(
    context: BrowserContext, server: BaseTestServer
) -> None:
    page = await context.new_page()
    await page.goto(str(server.make_url("/console")))
    await page.locator("#connect").click()
    await expect(page.locator("#connect")).to_have_text("Disconnect")
    await page.route("**/mcp?*", lambda route: route.fulfill(status=503, body="Unavailable"))
    requests: list[Request] = []
    page.on("request", lambda request: requests.append(request))
    await page.reload()
    await expect(page.locator("#status")).to_have_class("status off")
    await expect(page.locator("#connect")).to_have_text("Connect")
    assert len([request for request in requests if request.method == "POST"]) == 1
    await page.unroute("**/mcp?*")
    requests.clear()
    await page.reload()
    await expect(page.locator("#connect")).to_have_text("Connect")
    assert not [request for request in requests if request.method == "POST"]
