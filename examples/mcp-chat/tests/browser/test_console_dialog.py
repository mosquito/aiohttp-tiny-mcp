from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from aiohttp.test_utils import BaseTestServer, TestServer
from playwright.async_api import Browser, Page, Request, async_playwright, expect

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
    async with TestServer(create_app(tmp_path / "console.sqlite3")) as running:
        try:
            yield running
        finally:
            for context in browser.contexts:
                await context.close()


async def open_console(browser: Browser, server: BaseTestServer, endpoint: str) -> Page:
    page = await browser.new_page(viewport={"width": 1440, "height": 900})
    await page.add_init_script(
        "if (localStorage.getItem('mcp-console-endpoint:/console') === null) {"
        "localStorage.setItem('mcp-console-endpoint:/console', " + json.dumps(endpoint) + ");}"
    )
    await page.goto(str(server.make_url("/console")))
    return page


async def methods(page: Page) -> list[str]:
    return await page.locator("#auth-method option").evaluate_all(
        "options => options.map(option => option.value)"
    )


async def test_endpoint_fields_stay_in_dialog_and_cancel_preserves_connection(
    browser: Browser, server: BaseTestServer
) -> None:
    endpoint = "/mcp?org=example.com&user=human"
    page = await open_console(browser, server, endpoint)
    await page.locator("#connect").click()
    await expect(page.locator("#connect")).to_have_text("Disconnect")
    await expect(page.locator("#endpoint")).to_be_visible()
    await expect(page.locator("#endpoint")).to_have_value(endpoint)
    await expect(page.locator(".bar #endpoint-select")).to_have_count(0)
    await expect(page.locator(".bar #endpoint-parameters")).to_have_count(0)
    for cancel in ("button", "escape"):
        await page.locator("#authenticate").click()
        await expect(page.locator("#authentication")).to_be_visible()
        await expect(page.locator('#endpoint-parameters [data-name="org"]')).to_have_value(
            "example.com"
        )
        await expect(page.locator('#endpoint-parameters [data-name="user"]')).to_have_value("human")
        assert await methods(page) == ["none"]
        assert await page.locator("#auth-method").evaluate(
            "node => Boolean(node.compareDocumentPosition("
            "document.getElementById('endpoint-parameters')) & Node.DOCUMENT_POSITION_FOLLOWING)"
        )
        method_box = await page.locator("#auth-method").bounding_box()
        parameter_box = await page.locator("#endpoint-parameters").bounding_box()
        assert method_box is not None and parameter_box is not None
        assert method_box["y"] < parameter_box["y"]
        await expect(page.locator("#auth-secret")).not_to_be_visible()
        await page.locator('#endpoint-parameters [data-name="org"]').fill("discarded.example")
        await page.locator('#endpoint-parameters [data-name="user"]').fill("discarded")
        if cancel == "button":
            await page.locator("#auth-cancel").click()
        else:
            await page.keyboard.press("Escape")
        await expect(page.locator("#authentication")).not_to_be_visible()
        await expect(page.locator("#endpoint")).to_have_value(endpoint)
        await expect(page.locator("#connect")).to_have_text("Disconnect")


async def test_dialog_applies_query_without_auth_header_and_preserves_extra_parameters(
    browser: Browser, server: BaseTestServer
) -> None:
    page = await open_console(browser, server, "/mcp?org=example.com&user=human&trace=1")
    await page.locator("#connect").click()
    await expect(page.locator("#connect")).to_have_text("Disconnect")
    requests: list[Request] = []
    page.on("request", lambda request: requests.append(request))
    await page.locator("#authenticate").click()
    await page.locator('#endpoint-parameters [data-name="org"]').fill("other.example")
    await page.locator('#endpoint-parameters [data-name="user"]').fill("bob")
    await page.locator('#auth-form button[type="submit"]').click()
    await expect(page.locator("#authentication")).not_to_be_visible()
    await expect(page.locator("#connect")).to_have_text("Disconnect")
    query = parse_qs(urlsplit(await page.locator("#endpoint").input_value()).query)
    assert query == {"org": ["other.example"], "user": ["bob"], "trace": ["1"]}
    sent = [request for request in requests if request.method == "POST"]
    assert sent
    assert all(
        parse_qs(urlsplit(request.url).query)["org"] == ["other.example"] for request in sent
    )
    assert all("authorization" not in request.headers for request in sent)


async def test_manual_url_accepts_extra_query_parameters_with_declarations(
    browser: Browser, server: BaseTestServer
) -> None:
    page = await open_console(browser, server, "/mcp?org=example.com&user=human")
    await page.locator("#connect").click()
    await expect(page.locator("#connect")).to_have_text("Disconnect")
    await page.locator("#connect").click()
    await expect(page.locator("#endpoint")).to_be_editable()
    endpoint = "/mcp?org=manual.example&user=operator&extra=a%26b"
    await page.locator("#endpoint").fill(endpoint)
    await page.locator("#connect").click()
    await expect(page.locator("#connect")).to_have_text("Disconnect")
    await expect(page.locator("#endpoint")).to_have_value(endpoint)
    await page.locator("#authenticate").click()
    await expect(page.locator('#endpoint-parameters [data-name="org"]')).to_have_value(
        "manual.example"
    )
    await expect(page.locator('#endpoint-parameters [data-name="user"]')).to_have_value("operator")
    await page.locator("#auth-cancel").click()
    await expect(page.locator("#endpoint")).to_have_value(endpoint)


@pytest.mark.parametrize("method", ["bearer", "query"])
async def test_jwt_console_uses_declared_methods_and_cancel_preserves_credentials(
    browser: Browser, tmp_path: Path, method: str
) -> None:
    secret = "console-dialog-secret-with-at-least-32-bytes"
    token = issue_token(secret, "example.com", "alice")
    async with TestServer(create_app(tmp_path / "jwt.sqlite3", jwt_secret=secret)) as running:
        page = await open_console(browser, running, "/mcp")
        requests: list[Request] = []
        page.on("request", lambda request: requests.append(request))
        try:
            await page.locator("#connect").click()
            await expect(page.locator("#authentication")).to_be_visible()
            assert await methods(page) == ["bearer", "query"]
            await expect(page.locator('#endpoint-parameters [data-name="org"]')).to_have_count(0)
            await expect(page.locator('#endpoint-parameters [data-name="user"]')).to_have_count(0)
            await expect(page.locator("#auth-oauth")).not_to_be_visible()
            await page.locator("#auth-method").select_option(method)
            if method == "bearer":
                await page.locator("#auth-secret").fill(token)
            else:
                await page.locator("#endpoint-parameters .null-toggle input").uncheck()
                await page.locator('#endpoint-parameters [data-name="token"]').fill(token)
                await expect(page.locator("#auth-secret")).not_to_be_visible()
            requests.clear()
            await page.locator('#auth-form button[type="submit"]').click()
            await expect(page.locator("#authentication")).not_to_be_visible()
            await expect(page.locator("#connect")).to_have_text("Disconnect")
            endpoint = await page.locator("#endpoint").input_value()
            assert ("token=" in endpoint) == (method == "query")
            storage: Any = await page.evaluate("JSON.stringify({...localStorage})")
            assert token in storage
            sent = [request for request in requests if request.method == "POST"]
            assert sent
            expected = f"Bearer {token}" if method == "bearer" else None
            assert all(request.headers.get("authorization") == expected for request in sent)
            await page.reload()
            await expect(page.locator("#connect")).to_have_text("Disconnect")
            await expect(page.locator("#authentication")).not_to_be_visible()
            await expect(page.locator("#endpoint")).to_have_value(endpoint)
            await page.locator("#authenticate").click()
            await page.locator("#auth-method").select_option(
                "query" if method == "bearer" else "bearer"
            )
            await page.locator("#auth-cancel").click()
            await expect(page.locator("#endpoint")).to_have_value(endpoint)
            requests.clear()
            async with page.expect_request(lambda request: request.method == "POST"):
                await page.locator("#refresh").click()
            assert requests[-1].headers.get("authorization") == expected
            await page.locator("#authenticate").click()
            await page.locator("#auth-clear").click()
            storage = await page.evaluate("JSON.stringify({...localStorage})")
            assert token not in storage
        finally:
            await page.close()


async def test_console_starts_disconnected_and_restores_last_endpoint(
    browser: Browser, server: BaseTestServer
) -> None:
    page = await browser.new_page()
    requests: list[Request] = []
    page.on("request", lambda request: requests.append(request))
    await page.goto(str(server.make_url("/console")))
    await expect(page.locator("#connect")).to_have_text("Connect")
    await expect(page.locator("#endpoint")).to_be_editable()
    assert not [request for request in requests if request.method == "POST"]
    endpoint = "/mcp?org=restored.example&user=reader&extra=1"
    await page.locator("#endpoint").fill(endpoint)
    await page.locator("#connect").click()
    await expect(page.locator("#connect")).to_have_text("Disconnect")
    assert any(request.method == "POST" for request in requests)
    await page.locator("#connect").click()
    requests.clear()
    await page.reload()
    await expect(page.locator("#connect")).to_have_text("Connect")
    await expect(page.locator("#endpoint")).to_be_editable()
    await expect(page.locator("#endpoint")).to_have_value(endpoint)
    assert not [request for request in requests if request.method == "POST"]
    await page.locator("#connect").click()
    await expect(page.locator("#connect")).to_have_text("Disconnect")
    assert any(request.method == "POST" for request in requests)
