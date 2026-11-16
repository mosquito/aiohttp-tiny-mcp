from __future__ import annotations

import json
from collections.abc import AsyncIterator

import pytest
from aiohttp.test_utils import BaseTestServer, TestServer
from aiohttp_tiny_mcp import Endpoint, Registry
from aiohttp_tiny_mcp.console import Console, ConsoleEndpoint
from playwright.async_api import Browser, Page, Request, async_playwright, expect
from pydantic import BaseModel

pytestmark = pytest.mark.timeout(60)


class TenantQuery(BaseModel):
    tenant: str = "A"


@pytest.fixture
async def browser() -> AsyncIterator[Browser]:
    async with async_playwright() as engine:
        running = await engine.chromium.launch()
        try:
            yield running
        finally:
            await running.close()


async def open_console(
    browser: Browser, server: BaseTestServer, endpoint: str, saved: dict[str, str]
) -> Page:
    page = await browser.new_page()
    settings = json.dumps({"endpoint": endpoint, "saved": saved})
    await page.add_init_script(
        "const settings = "
        + settings
        + "; localStorage.setItem('mcp-console-endpoint:/console', settings.endpoint);"
        "for (const [path, secret] of Object.entries(settings.saved)) {"
        "const endpoint = new URL(path, location.origin).href;"
        "const key = `mcp-console-credentials:${JSON.stringify(['/console', endpoint])}`;"
        "localStorage.setItem(key, JSON.stringify({version:1, endpoint, method:'bearer',"
        "username:'', secret, header:''}));}"
    )
    await page.goto(str(server.make_url("/console")))
    await expect(page.locator("#connect")).to_have_text("Connect")
    await page.locator("#connect").click()
    await expect(page.locator("#connect")).to_have_text("Disconnect")
    return page


@pytest.mark.parametrize("saved_target", [False, True])
async def test_query_edit_restores_only_credentials_for_the_exact_endpoint(
    browser: Browser, saved_target: bool
) -> None:
    app = Endpoint(Registry("scope-review", "1")).app()
    Console(endpoints=[ConsoleEndpoint("/mcp", "Tenant", TenantQuery, auth=("bearer",))]).setup(app)
    saved = {"/mcp?tenant=A": "token-A"}
    if saved_target:
        saved["/mcp?tenant=B"] = "token-B"
    async with TestServer(app) as server:
        page = await open_console(browser, server, "/mcp?tenant=A", saved)
        try:
            await page.locator("#authenticate").click()
            await expect(page.locator("#auth-secret")).to_have_value("token-A")
            await page.locator('#endpoint-parameters [data-name="tenant"]').fill("B")
            await expect(page.locator("#auth-secret")).to_have_value(
                "token-B" if saved_target else ""
            )
            requests: list[Request] = []
            page.on("request", lambda request: requests.append(request))
            if saved_target:
                await page.locator('#auth-form button[type="submit"]').click()
                await expect(page.locator("#authentication")).not_to_be_visible()
                await expect(page.locator("#connect")).to_have_text("Disconnect")
                expected_endpoint = "/mcp?tenant=B"
                expected_token = "token-B"
            else:
                await page.locator("#auth-cancel").click()
                async with page.expect_request(lambda request: request.method == "POST"):
                    await page.locator("#refresh").click()
                expected_endpoint = "/mcp?tenant=A"
                expected_token = "token-A"
            await expect(page.locator("#endpoint")).to_have_value(expected_endpoint)
            sent = [request for request in requests if request.method == "POST"]
            assert sent
            assert all(request.url.endswith(expected_endpoint) for request in sent)
            assert all(
                request.headers.get("authorization") == f"Bearer {expected_token}"
                for request in sent
            )
        finally:
            await page.close()


async def test_dialog_matches_declarations_with_the_same_path_and_distinct_fixed_query(
    browser: Browser,
) -> None:
    app = Endpoint(Registry("selection-review", "1")).app()
    Console(
        endpoints=[
            ConsoleEndpoint("/mcp?mode=public", "Public", auth=("none",)),
            ConsoleEndpoint("/mcp?mode=private", "Private", auth=("bearer",)),
        ]
    ).setup(app)
    async with TestServer(app) as server:
        endpoint = "/mcp?mode=private&extra=kept"
        page = await open_console(browser, server, endpoint, {endpoint: "private-token"})
        try:
            await page.locator("#authenticate").click()
            await expect(page.locator("#endpoint-select")).to_have_value("1")
            await expect(page.locator("#endpoint-description")).to_have_text("Private")
            await expect(page.locator("#auth-method")).to_have_value("bearer")
            await expect(page.locator("#auth-secret")).to_have_value("private-token")
            await page.locator("#auth-cancel").click()
            await expect(page.locator("#endpoint")).to_have_value(endpoint)
        finally:
            await page.close()


async def test_oauth_only_dialog_preserves_restored_bearer_token_on_reconnect(
    browser: Browser,
) -> None:
    app = Endpoint(Registry("oauth-review", "1")).app()
    Console(
        endpoints=[ConsoleEndpoint("/mcp", "OAuth only", auth=("oauth",))],
        oauth_client_id="console-client",
    ).setup(app)
    async with TestServer(app) as server:
        page = await open_console(browser, server, "/mcp", {"/mcp": "saved-oauth-token"})
        try:
            requests: list[Request] = []
            page.on("request", lambda request: requests.append(request))
            await page.locator("#authenticate").click()
            await expect(page.locator("#auth-method")).to_have_value("oauth")
            await expect(page.locator("#auth-oauth")).not_to_be_visible()
            await page.locator('#auth-form button[type="submit"]').click()
            await expect(page.locator("#authentication")).not_to_be_visible()
            await expect(page.locator("#connect")).to_have_text("Disconnect")
            sent = [request for request in requests if request.method == "POST"]
            assert sent
            assert all(
                request.headers.get("authorization") == "Bearer saved-oauth-token"
                for request in sent
            )
            record = await page.evaluate(
                "JSON.parse(localStorage.getItem(`mcp-console-credentials:"
                "${JSON.stringify(['/console', location.origin + '/mcp'])}`))"
            )
            assert record["method"] == "bearer"
            assert record["secret"] == "saved-oauth-token"
        finally:
            await page.close()
