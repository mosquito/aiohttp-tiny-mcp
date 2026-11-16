from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from aiohttp.test_utils import BaseTestServer, TestServer
from playwright.async_api import Browser, Request, async_playwright, expect

from mcp_chat.auth import issue_token
from mcp_chat.server import create_app

pytestmark = pytest.mark.timeout(60)


@pytest.fixture
async def browser() -> AsyncIterator[Browser]:
    async with async_playwright() as engine:
        running = await engine.chromium.launch()
        try:
            yield running
        finally:
            await running.close()


@pytest.fixture
async def server(tmp_path: Path, browser: Browser) -> AsyncIterator[BaseTestServer]:
    async with TestServer(create_app(tmp_path / "reconnect.sqlite3")) as running:
        try:
            yield running
        finally:
            for context in browser.contexts:
                await context.close()


async def test_reload_restores_connection_and_options_but_new_tab_stays_disconnected(
    browser: Browser, server: BaseTestServer
) -> None:
    context = await browser.new_context()
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
    browser: Browser, tmp_path: Path, method: str
) -> None:
    secret = "console-reconnect-secret-at-least-32-bytes"
    token = issue_token(secret, "example.com", "reader")
    async with TestServer(create_app(tmp_path / "auth.sqlite3", jwt_secret=secret)) as running:
        page = await browser.new_page()
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
    browser: Browser, server: BaseTestServer
) -> None:
    page = await browser.new_page()
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
