from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from aiohttp.test_utils import BaseTestServer, TestServer
from playwright.async_api import Browser, Page, Request, async_playwright, expect

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
    async with TestServer(create_app(tmp_path / "console-agent.sqlite3")) as running:
        try:
            yield running
        finally:
            for context in browser.contexts:
                await context.close()


async def open_console(browser: Browser, server: BaseTestServer) -> tuple[Page, list[Request]]:
    page = await browser.new_page(viewport={"width": 1440, "height": 900})
    requests: list[Request] = []
    page.on("request", lambda request: requests.append(request))
    await page.goto(str(server.make_url("/console")))
    await expect(page.locator("#connect")).to_have_text("Connect")
    return page, requests


async def generated_server(page: Page) -> dict:
    config = json.loads(await page.locator("#agent-code").input_value())
    return config["mcpServers"]["mcp_chat"]


async def test_agent_dialog_defaults_to_user_and_copies_without_connecting(
    browser: Browser, server: BaseTestServer
) -> None:
    page, requests = await open_console(browser, server)
    await page.evaluate(
        "Object.defineProperty(navigator, 'clipboard', {configurable: true, value: {"
        "writeText: async text => {window.copiedConfiguration = text;}}})"
    )
    endpoint = await page.locator("#endpoint").input_value()
    await page.locator("#add-agent").click()
    await expect(page.locator("#agent-setup")).to_be_visible()
    for width, height in ((1440, 900), (390, 844)):
        await page.set_viewport_size({"width": width, "height": height})
        bounds = await page.locator("#agent-setup").bounding_box()
        assert bounds is not None
        assert 0 <= bounds["x"] <= width - bounds["width"]
        assert 0 <= bounds["y"] <= height - bounds["height"]
        link = await page.locator("#agent-docs").bounding_box()
        close = await page.locator("#agent-close").bounding_box()
        copy = await page.locator("#agent-copy").bounding_box()
        assert link is not None and close is not None and copy is not None
        for control in (link, close, copy):
            assert bounds["x"] <= control["x"]
            assert control["x"] + control["width"] <= bounds["x"] + bounds["width"]
        if width == 1440:
            assert link["x"] + link["width"] < close["x"] < copy["x"]
            assert abs(link["y"] + link["height"] / 2 - close["y"] - close["height"] / 2) < 1
    await expect(page.locator("#agent-method")).to_have_value("mcp-json")
    await expect(page.locator("#agent-level")).to_have_value("user")
    await expect(page.locator("#agent-method option:checked")).to_have_text("Claude Code")
    await expect(page.get_by_role("combobox", name="Agent", exact=True)).to_have_value("mcp-json")
    await expect(page.locator("#agent-name")).to_have_value("mcp_chat")
    await expect(page.locator("#agent-code")).to_have_attribute("readonly", "")
    await expect(page.locator("#agent-target")).to_have_text("Configuration file: ~/.claude.json")
    url = str(server.make_url(endpoint))
    await expect(page.locator("#agent-endpoint")).to_have_text(url)
    assert await generated_server(page) == {"type": "http", "url": url}
    await expect(page.locator("#agent-headers-field")).not_to_be_visible()
    code = await page.locator("#agent-code").input_value()
    await page.locator("#agent-copy").click()
    await expect(page.locator("#agent-copy-status")).to_have_text("Configuration copied.")
    assert await page.evaluate("window.copiedConfiguration") == code
    await page.evaluate(
        "() => {navigator.clipboard.writeText = async () => {"
        "throw new Error('Clipboard blocked');};}"
    )
    await page.locator("#agent-copy").click()
    await expect(page.locator("#agent-copy-status")).to_have_text(
        "Copy the selected configuration with your keyboard."
    )
    assert await page.locator("#agent-code").evaluate(
        "field => field.selectionStart === 0 && field.selectionEnd === field.value.length"
    )
    await expect(page.locator("#agent-setup")).to_be_visible()
    await expect(page.locator("#connect")).to_have_text("Connect")
    for close in ("button", "escape"):
        if close == "button":
            await page.locator("#agent-close").click()
        else:
            await page.keyboard.press("Escape")
        await expect(page.locator("#agent-setup")).not_to_be_visible()
        await expect(page.locator("#endpoint")).to_have_value(endpoint)
        await expect(page.locator("#connect")).to_have_text("Connect")
        if close == "button":
            await page.locator("#add-agent").click()
    assert not [request for request in requests if request.method == "POST"]


async def test_agent_dialog_switches_formats_and_rejects_invalid_names(
    browser: Browser, server: BaseTestServer
) -> None:
    page, requests = await open_console(browser, server)
    await page.locator("#add-agent").click()
    outputs: dict[str, str] = {}
    for method in ("mcp-json", "codex", "cursor", "copilot", "gemini", "pi", "opencode"):
        await page.locator("#agent-method").select_option(method)
        code = await page.locator("#agent-code").input_value()
        assert code
        assert await page.locator("#agent-target").inner_text()
        assert (await page.locator("#agent-docs").get_attribute("href") or "").startswith(
            "https://"
        )
        if method == "codex":
            assert '[mcp_servers."mcp_chat"]' in code
            await expect(page.locator("#agent-target")).to_have_text(
                "Configuration file: ~/.codex/config.toml"
            )
        else:
            assert isinstance(json.loads(code), dict)
        outputs[method] = code
    assert outputs["mcp-json"] != outputs["codex"]
    assert outputs["mcp-json"] != outputs["gemini"]
    assert outputs["mcp-json"] != outputs["opencode"]
    await page.locator("#agent-method").select_option("mcp-json")
    await page.locator("#agent-name").fill("invalid name")
    await expect(page.locator("#agent-code")).to_have_value("")
    await expect(page.locator("#agent-copy")).to_be_disabled()
    await expect(page.locator("#agent-docs")).to_be_hidden()
    await page.locator("#agent-name").fill("custom_agent")
    await expect(page.locator("#agent-copy")).to_be_enabled()
    config = json.loads(await page.locator("#agent-code").input_value())
    assert list(config["mcpServers"]) == ["custom_agent"]
    assert not [request for request in requests if request.method == "POST"]


async def test_agent_dialog_uses_latest_manual_url_and_preserves_query_tokens(
    browser: Browser, server: BaseTestServer
) -> None:
    page, requests = await open_console(browser, server)
    for endpoint in (
        "/mcp?org=manual.example&user=operator&extra=a%26b",
        "/mcp?token=query-test-token&extra=one%20two",
    ):
        await page.locator("#endpoint").fill(endpoint)
        await page.locator("#add-agent").click()
        generated = await generated_server(page)
        assert generated["url"] == str(server.make_url(endpoint))
        assert "headers" not in generated
        await page.locator("#agent-close").click()
        await expect(page.locator("#endpoint")).to_have_value(endpoint)
    assert not [request for request in requests if request.method == "POST"]


async def test_agent_dialog_exports_saved_headers_only_for_the_exact_endpoint(
    browser: Browser, tmp_path: Path
) -> None:
    secret = "agent-setup-jwt-secret-at-least-32-bytes"
    async with TestServer(create_app(tmp_path / "jwt-agent.sqlite3", jwt_secret=secret)) as running:
        page, requests = await open_console(browser, running)
        endpoint = str(running.make_url("/mcp"))
        token = "saved-agent-test-token"
        await page.evaluate(
            "({endpoint, token}) => localStorage.setItem("
            "'mcp-console-credentials:' + JSON.stringify([location.pathname, endpoint]),"
            "JSON.stringify({version: 1, endpoint, method: 'bearer', username: '',"
            "secret: token, header: ''}))",
            {"endpoint": endpoint, "token": token},
        )
        await page.reload()
        await page.locator("#add-agent").click()
        await expect(page.locator("#agent-headers-field")).to_be_visible()
        await expect(page.locator("#agent-headers")).to_be_checked()
        assert (await generated_server(page))["headers"] == {"Authorization": f"Bearer {token}"}
        await page.locator("#agent-headers").uncheck()
        await page.locator("#agent-level").select_option("project")
        await expect(page.locator("#agent-headers")).not_to_be_checked()
        assert "headers" not in await generated_server(page)
        assert token not in await page.locator("#agent-code").input_value()
        await page.locator("#agent-headers").check()
        assert (await generated_server(page))["headers"] == {"Authorization": f"Bearer {token}"}
        await page.locator("#agent-close").click()
        await page.locator("#endpoint").fill("/mcp?scope=other")
        await page.locator("#add-agent").click()
        await expect(page.locator("#agent-headers-field")).not_to_be_visible()
        assert "headers" not in await generated_server(page)
        assert token not in await page.locator("#agent-code").input_value()
        await expect(page.locator("#connect")).to_have_text("Connect")
        assert not [request for request in requests if request.method == "POST"]
        await page.close()


async def test_agent_configuration_levels_update_paths_and_preserve_values(
    browser: Browser, server: BaseTestServer
) -> None:
    page, requests = await open_console(browser, server)
    await page.evaluate(
        "Object.defineProperty(navigator, 'clipboard', {configurable: true, value: {"
        "writeText: async text => {window.copiedConfiguration = text;}}})"
    )
    await page.locator("#add-agent").click()
    await page.locator("#agent-name").fill("my_agent")
    paths = {
        "mcp-json": (".mcp.json", "~/.claude.json"),
        "codex": (".codex/config.toml", "~/.codex/config.toml"),
        "cursor": (".cursor/mcp.json", "~/.cursor/mcp.json"),
        "copilot": (".mcp.json", "~/.copilot/mcp-config.json"),
        "gemini": (".gemini/settings.json", "~/.gemini/settings.json"),
        "pi": (".pi/mcp.json", "~/.pi/agent/mcp.json"),
        "opencode": ("opencode.json", "~/.config/opencode/opencode.json"),
    }
    for method, (project_path, user_path) in paths.items():
        await page.locator("#agent-method").select_option(method)
        for level, path in (("user", user_path), ("project", project_path)):
            await page.locator("#agent-level").select_option(level)
            await expect(page.locator("#agent-method")).to_have_value(method)
            await expect(page.locator("#agent-name")).to_have_value("my_agent")
            await expect(page.locator("#agent-target")).to_have_text(f"Configuration file: {path}")
            await expect(page.locator("#agent-instructions")).to_contain_text(path)
            await page.locator("#agent-copy").click()
            assert (
                await page.evaluate("window.copiedConfiguration")
                == await page.locator("#agent-code").input_value()
            )
    await page.locator("#agent-close").click()
    await page.locator("#add-agent").click()
    await expect(page.locator("#agent-level")).to_have_value("project")
    await page.reload()
    await page.locator("#add-agent").click()
    await expect(page.locator("#agent-level")).to_have_value("project")
    await page.locator("#agent-level").select_option("user")
    await page.reload()
    await page.locator("#add-agent").click()
    await expect(page.locator("#agent-level")).to_have_value("user")
    await expect(page.locator("#connect")).to_have_text("Connect")
    assert not [request for request in requests if request.method == "POST"]


@pytest.mark.parametrize("storage", ["invalid", "unavailable", "write-denied"])
async def test_agent_level_storage_failures_use_user(
    browser: Browser, server: BaseTestServer, storage: str
) -> None:
    page, requests = await open_console(browser, server)
    if storage == "invalid":
        await page.evaluate(
            "localStorage.setItem('mcp-console-agent-level:' + location.pathname, 'global')"
        )
    elif storage == "unavailable":
        await page.evaluate(
            "Object.defineProperty(window, 'localStorage', {get() {"
            "throw new DOMException('Storage blocked', 'SecurityError');}})"
        )
    else:
        await page.evaluate(
            "() => {Storage.prototype.setItem = () => {"
            "throw new DOMException('Storage full', 'QuotaExceededError');};}"
        )
    await page.locator("#add-agent").click()
    await expect(page.locator("#agent-level")).to_have_value("user")
    await page.locator("#agent-level").select_option("project")
    await expect(page.locator("#agent-target")).to_have_text("Configuration file: .mcp.json")
    if storage == "invalid":
        await page.evaluate(
            "localStorage.setItem('mcp-console-agent-level:' + location.pathname, 'invalid')"
        )
    await page.locator("#agent-close").click()
    await page.locator("#add-agent").click()
    await expect(page.locator("#agent-level")).to_have_value("user")
    await expect(page.locator("#agent-copy")).to_be_enabled()
    assert not [request for request in requests if request.method == "POST"]


async def test_agent_level_is_scoped_to_console_path_and_browser_storage(
    browser: Browser, server: BaseTestServer
) -> None:
    page, requests = await open_console(browser, server)
    await page.evaluate("localStorage.setItem('mcp-console-agent-level:/other-console', 'project')")
    await page.locator("#add-agent").click()
    await expect(page.locator("#agent-level")).to_have_value("user")
    await page.locator("#agent-level").select_option("project")
    assert (
        await page.evaluate("localStorage.getItem('mcp-console-agent-level:' + location.pathname)")
        == "project"
    )
    other, other_requests = await open_console(browser, server)
    await other.locator("#add-agent").click()
    await expect(other.locator("#agent-level")).to_have_value("user")
    assert not [request for request in requests + other_requests if request.method == "POST"]
