from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

import aiohttp
import jwt
import pytest
from aiohttp.test_utils import BaseTestServer, TestServer
from aiohttp_tiny_mcp import AdapterSet, CallToolResult, Client
from playwright.async_api import Browser, Page, Route, async_playwright, expect

from mcp_chat.server import create_app

pytestmark = pytest.mark.timeout(60)
MODERN = AdapterSet.default().by_version["2026-07-28"]


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
    async with TestServer(create_app(tmp_path / "browser.sqlite3")) as running:
        try:
            yield running
        finally:
            for context in browser.contexts:
                await context.close()


async def open_chat(browser: Browser, server: BaseTestServer, user: str = "alice") -> Page:
    page = await browser.new_page()
    await page.goto(str(server.make_url(f"/ui/?org=example.com&user={user}")))
    await expect(page.locator("#channels")).to_contain_text("general")
    return page


async def channel(page: Page, name: str) -> None:
    await page.locator(f'#channels button[data-channel="{name}"]').click()
    await expect(page.locator("#view-title")).to_contain_text(name)


async def call(client: Client, tool: str, **arguments: Any) -> dict[str, Any]:
    result = await client.call_tool(tool, arguments)
    assert isinstance(result, CallToolResult)
    assert not result.is_error, result
    assert isinstance(result.structured_content, dict)
    return result.structured_content


async def test_chat_workflow_uses_mcp_and_polls_inbox(
    browser: Browser, server: BaseTestServer
) -> None:
    alice = await open_chat(browser, server)
    bob = await open_chat(browser, server, "bob")
    failures: list[str] = []
    requests: list[dict[str, Any]] = []
    alice.on("pageerror", lambda error: failures.append(str(error)))
    bob.on("pageerror", lambda error: failures.append(str(error)))

    def record(request: Any) -> None:
        if request.method == "POST":
            assert urlsplit(request.url).path == "/mcp"
            requests.append(request.post_data_json)

    alice.on("request", record)
    bob.on("request", record)
    await alice.locator("#channel-create-open").click()
    await alice.locator("#channel-name").fill("release")
    await alice.locator("#channel-description").fill("Release coordination")
    await alice.locator("#channel-create").click()
    await expect(alice.locator("#view-title")).to_contain_text("release")
    await expect(bob.locator("#channels")).to_contain_text("release", timeout=10000)
    await channel(bob, "release")
    await bob.locator("#subscribe-toggle").click()
    await expect(bob.locator("#subscribe-toggle")).to_have_text("Leave channel")
    await bob.locator("#inbox-open").click()
    await alice.locator("#message-text").fill("Release is ready for review.")
    await alice.locator("#send-message").click()
    await expect(bob.locator("#messages")).to_contain_text(
        "Release is ready for review.", timeout=10000
    )
    await expect(bob.locator("#inbox-badge")).to_have_text("1")
    async with Client(str(server.make_url("/mcp?org=example.com&user=bob")), MODERN) as client:
        unread = (await call(client, "inbox"))["messages"]
        assert len(unread) == 1
        root_id = unread[0]["id"]

    await (
        bob.locator(f'#messages [data-message-id="{root_id}"]')
        .get_by_role("button", name=re.compile("[Tt]hread|[Rr]epl"))
        .click()
    )
    await bob.locator("#reply-text").fill("Reviewed and approved.")
    await bob.locator("#reply-send-to-channel").check()
    await bob.locator("#send-reply").click()
    await expect(bob.locator("#thread-messages")).to_contain_text("Reviewed and approved.")
    await expect(alice.locator("#messages")).to_contain_text(
        "Reviewed and approved.", timeout=10000
    )
    await bob.locator("#thread-close").click()
    await bob.locator("#mark-read").click()
    await expect(bob.locator("#inbox-badge")).to_have_text("0")

    await alice.locator("#search-open").click()
    await alice.locator("#search-query").fill("approved")
    await alice.locator("#search-form").get_by_role("button", name="Search", exact=True).click()
    await expect(alice.locator("#messages")).to_contain_text("Reviewed and approved.")
    await alice.locator("#rules-open").click()
    await expect(alice.locator("#rules-content")).to_contain_text("post_message")
    await expect(alice.locator("#rules-content")).to_contain_text("mark_read")
    assert any(
        r["method"] == "resources/read" and r["params"]["uri"] == "chat://rules" for r in requests
    )
    assert sum(r.get("params", {}).get("name") == "inbox" for r in requests) >= 2
    assert not failures


async def test_observe_pages_agent_activity_and_receives_updates(
    browser: Browser, server: BaseTestServer
) -> None:
    async with Client(str(server.make_url("/mcp?org=example.com&user=agent")), MODERN) as client:
        await call(client, "subscribe", channel="tasks")
        for number in range(52):
            await call(client, "post_message", channel="tasks", text=f"Agent activity {number}")
        await call(client, "send_dm", user="bob", text="Private audit message")
        page = await browser.new_page()
        await page.goto(str(server.make_url("/observe?org=example.com&user=agent&view=all")))
        await expect(page.get_by_role("table", name="Agent activity")).to_be_visible()
        await expect(page.locator("#observe-events [data-event-id]")).to_have_count(50)
        await expect(page.locator("#observe-events")).to_contain_text("Private audit message")
        await expect(page.locator(".observe-event-summary").first).to_contain_text("To @bob (DM)")
        row = page.locator(".observe-event-row").first
        assert await row.evaluate("node => node.getBoundingClientRect().height") <= 40
        detail = page.locator("[data-event-id]").first
        event_id = await detail.get_attribute("data-event-id")
        await detail.locator(".observe-toggle").click()
        await expect(detail.locator(".observe-event-details")).to_be_visible()
        await page.locator("#observe-more").click()
        await expect(page.locator("#observe-events [data-event-id]")).to_have_count(54)
        await expect(page.locator("#observe-events")).to_contain_text("subscribe")
        await expect(page.locator("#observe-events")).to_contain_text("Subscribed to #tasks")
        await expect(page.locator("#observe-more")).not_to_be_visible()
        await call(client, "post_message", channel="tasks", text="Live audit event")
        await expect(page.locator("#observe-events")).to_contain_text(
            "Live audit event", timeout=10000
        )
        await expect(page.locator("#observe-events [data-event-id]")).to_have_count(55)
        await expect(page.locator(f"#observe-details-{event_id}")).to_be_visible()
        await page.locator("#observe-user").fill("nobody")
        await page.locator("#observe-apply").click()
        await expect(page.locator("#observe-events [data-event-id]")).to_have_count(0)
        await expect(page.locator("#observe-events")).not_to_contain_text("Private audit message")
        await page.set_viewport_size({"width": 390, "height": 844})
        assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")


async def test_default_browser_identity_is_human(browser: Browser, server: BaseTestServer) -> None:
    page = await browser.new_page()
    await page.goto(str(server.make_url("/ui/")))
    await expect(page.locator("#channels")).to_contain_text("general")
    await expect(page.locator("#user")).to_have_value("human")
    assert "user=human" in page.url
    await page.goto(str(server.make_url("/observe")))
    await expect(page.locator("#observe-user")).to_have_value("human")
    await expect(page.locator("#observe-title")).to_contain_text("human")


async def test_observe_tool_filter_defaults_and_bookmarks(
    browser: Browser, server: BaseTestServer
) -> None:
    async with Client(str(server.make_url("/mcp?org=example.com&user=agent")), MODERN) as client:
        await call(client, "subscribe", channel="tasks")
        await call(client, "post_message", channel="tasks", text="Channel conversation")
        await call(client, "send_dm", user="bob", text="Direct conversation")
        await call(client, "identity")
        page = await browser.new_page()
        await page.goto(str(server.make_url("/observe?org=example.com&user=agent")))
        await expect(page.locator("#observe-mode")).to_have_value("conversations")
        await expect(page.locator("[data-event-id]")).to_have_count(2)
        assert await page.evaluate("new URL(location.href).searchParams.getAll('tool')") == [
            "post_message",
            "send_dm",
        ]
        await expect(page.locator(".observe-event-name")).to_have_text(["send_dm", "post_message"])
        await call(client, "identity")
        await page.locator("#observe-mode").select_option("all")
        await expect(page.locator("[data-event-id]")).to_have_count(5)
        assert "view=all" in page.url
        await page.reload()
        await expect(page.locator("#observe-mode")).to_have_value("all")
        await expect(page.locator("[data-event-id]")).to_have_count(5)
        await page.goto(str(server.make_url("/observe?user=agent&tool=subscribe&tool=identity")))
        await expect(page.locator("#observe-mode")).to_have_value("custom")
        await expect(page.locator("[data-event-id]")).to_have_count(3)
        await expect(page.locator(".observe-event-name")).to_have_text(
            ["identity", "identity", "subscribe"]
        )
        await page.locator("#observe-user").fill("nobody")
        await page.locator("#observe-apply").click()
        await expect(page.locator("[data-event-id]")).to_have_count(0)
        assert await page.evaluate("new URL(location.href).searchParams.getAll('tool')") == [
            "subscribe",
            "identity",
        ]
        await page.set_viewport_size({"width": 390, "height": 844})
        assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")


async def test_observe_requires_admin_secret(browser: Browser, tmp_path: Path) -> None:
    secret = "observer-browser-signing-key-at-least-thirty-two-bytes"
    async with TestServer(create_app(tmp_path / "observe.sqlite3", jwt_secret=secret)) as server:
        page = await browser.new_page()
        await page.goto(str(server.make_url("/observe?org=example.com&user=agent")))
        await expect(page.locator("#observe-login-dialog")).to_be_visible()
        await page.locator("#observe-secret").fill(secret)
        await page.locator("#observe-login-submit").click()
        await expect(page.locator("#observe-login-dialog")).not_to_be_visible()
        await expect(page.locator("#observe-secret")).to_have_value("")
        await expect(page.locator("#observe-error")).not_to_be_visible()
        await page.reload()
        await expect(page.locator("#observe-title")).to_contain_text("agent")
        await expect(page.locator("#observe-login-dialog")).not_to_be_visible()
        assert secret not in await page.evaluate("JSON.stringify(localStorage)")
        await page.context.close()


async def test_live_updates_browser_notifications_and_polling_fallback(
    browser: Browser, server: BaseTestServer
) -> None:
    page = await browser.new_page()
    await page.add_init_script("""
        window.alerts = [];
        window.permissionRequests = 0;
        window.Notification = class {
          static permission = 'default';
          static async requestPermission() {
            window.permissionRequests += 1;
            this.permission = 'granted';
            return 'granted';
          }
          constructor(title, options) { window.alerts.push({title, ...options}); }
          close() {}
        };
        document.hasFocus = () => false;
    """)
    requests: list[str] = []
    page.on("request", lambda r: requests.append(r.url) if r.method == "POST" else None)
    await page.goto(str(server.make_url("/ui/?org=example.com&user=bob")))
    await expect(page.locator("#sync-note")).to_have_text("Live MCP updates")
    assert await page.evaluate("permissionRequests") == 0
    async with page.expect_response(
        lambda response: (
            response.request.method == "POST"
            and (response.request.post_data_json or {}).get("params", {}).get("name") == "inbox"
        )
    ) as pending:
        await page.locator("#notifications-enable").click()
    await (await pending.value).finished()
    await expect(page.locator("#notifications-enable")).to_have_text("Notifications enabled")
    await page.wait_for_timeout(300)
    requests.clear()
    await page.wait_for_timeout(3300)
    assert requests == [], "A healthy notification stream must not poll MCP tools."
    async with Client(str(server.make_url("/mcp?org=example.com&user=alice")), MODERN) as client:
        await call(client, "send_dm", user="bob", text="Incoming push notification")
        await expect(page.locator("#messages")).to_contain_text("Incoming push notification")
        await page.wait_for_function("alerts.length === 1")
        assert await page.evaluate("alerts[0].body") == "Incoming push notification"
        assert await page.evaluate("permissionRequests") == 1
        await page.locator("#refresh").click()
        await expect(page.locator("#connection-status")).to_have_text("Live")
        assert await page.evaluate("alerts.length") == 1

        async def reject_stream(route: Route) -> None:
            if route.request.method == "GET":
                await route.fulfill(status=503, body="Stream unavailable")
            else:
                await route.continue_()

        await page.route("**/mcp?**", reject_stream)
        await page.reload()
        await expect(page.locator("#sync-note")).to_contain_text("Polling fallback")
        await call(client, "send_dm", user="bob", text="Fallback update")
        await expect(page.locator("#messages")).to_contain_text("Fallback update", timeout=8000)
        await page.unroute("**/mcp?**", reject_stream)
        await expect(page.locator("#sync-note")).to_have_text("Live MCP updates", timeout=10000)


async def test_context_and_drafts_are_isolated(browser: Browser, server: BaseTestServer) -> None:
    page = await open_chat(browser, server)
    await channel(page, "general")
    await page.locator("#subscribe-toggle").click()
    await page.locator("#message-text").fill("Alice's unsent draft")
    await channel(page, "random")
    await channel(page, "general")
    await expect(page.locator("#message-text")).to_have_value("Alice's unsent draft")
    await page.reload()
    await expect(page.locator("#channels")).to_contain_text("general")
    await channel(page, "general")
    await expect(page.locator("#message-text")).to_have_value("Alice's unsent draft")

    await page.locator("#org").fill("other.example")
    await page.locator("#connect").click()
    await expect(page.locator("#workspace-name")).to_have_text("other.example")
    await channel(page, "general")
    await expect(page.locator("#subscribe-toggle")).to_have_text("Join channel")
    await expect(page.locator("#message-text")).to_have_value("")
    assert "org=other.example" in page.url
    await page.locator("#org").fill("example.com")
    await page.locator("#user").fill("bob")
    await page.locator("#connect").click()
    await expect(page.locator("#workspace-name")).to_have_text("example.com")
    await channel(page, "general")
    await expect(page.locator("#message-text")).to_have_value("")
    await page.locator("#user").fill("alice")
    await page.locator("#connect").click()
    await channel(page, "general")
    await expect(page.locator("#message-text")).to_have_value("Alice's unsent draft")


async def test_paging_does_not_acknowledge_unseen_messages(
    browser: Browser, server: BaseTestServer
) -> None:
    async with (
        Client(str(server.make_url("/mcp?org=example.com&user=alice")), MODERN) as alice,
        Client(str(server.make_url("/mcp?org=example.com&user=bob")), MODERN) as bob,
    ):
        await call(alice, "subscribe", channel="general")
        await call(bob, "subscribe", channel="general")
        for number in range(55):
            await call(alice, "post_message", channel="general", text=f"Update {number:02d}")
        page = await open_chat(browser, server, "bob")
        await expect(page.locator("#messages [data-message-id]")).to_have_count(50)
        await page.locator("#load-more").click()
        await expect(page.locator("#messages [data-message-id]")).to_have_count(55)
        await page.locator("#inbox-open").click()
        await expect(page.locator("#messages [data-message-id]")).to_have_count(50)
        await page.locator("#mark-read").click()
        await expect(page.locator("#inbox-badge")).to_have_text("5")
        unread = (await call(bob, "inbox"))["messages"]
        assert [row["text"] for row in unread] == [f"Update {i:02d}" for i in range(50, 55)]
        await channel(page, "general")
        await expect(page.locator("#messages [data-message-id]")).to_have_count(50)
        await page.locator("#load-more").click()
        await expect(page.locator("#messages [data-message-id]")).to_have_count(55)
        await call(alice, "post_message", channel="general", text="Update 55")
        await expect(page.locator("#messages [data-message-id]")).to_have_count(56, timeout=10000)
        await expect(page.locator("#messages")).to_contain_text("Update 00")


async def test_mobile_theme_and_message_text(browser: Browser, server: BaseTestServer) -> None:
    async with Client(str(server.make_url("/mcp?org=example.com&user=alice")), MODERN) as client:
        await call(client, "subscribe", channel="general")
        await call(
            client,
            "post_message",
            channel="general",
            text='<img src=x onerror="window.chatInjected=true"> ' + "longword" * 60,
        )
    page = await open_chat(browser, server)
    await page.set_viewport_size({"width": 390, "height": 844})
    await channel(page, "general")
    await expect(page.locator("#messages")).to_contain_text("<img src=x")
    assert await page.locator("#messages img").count() == 0
    assert await page.evaluate("window.chatInjected === undefined")
    for theme in ("light", "dark"):
        await page.emulate_media(color_scheme=theme)
        assert await page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    await page.locator("#rules-open").click()
    await expect(page.locator("#rules-dialog")).to_be_visible()
    await page.keyboard.press("Escape")
    await expect(page.locator("#rules-dialog")).not_to_be_visible()


async def test_switching_org_discards_delayed_inbox(
    browser: Browser, server: BaseTestServer
) -> None:
    for org, text in [
        ("example.com", "Old organization message"),
        ("other.example", "New organization message"),
    ]:
        async with (
            Client(str(server.make_url(f"/mcp?org={org}&user=alice")), MODERN) as alice,
            Client(str(server.make_url(f"/mcp?org={org}&user=bob")), MODERN) as bob,
        ):
            await call(alice, "subscribe", channel="general")
            await call(bob, "subscribe", channel="general")
            await call(alice, "post_message", channel="general", text=text)
    page = await open_chat(browser, server, "bob")
    await expect(page.locator("#messages")).to_contain_text("Old organization message")
    started = asyncio.Event()
    release = asyncio.Event()
    completed = asyncio.Event()

    async def delay_inbox(route: Route) -> None:
        body = route.request.post_data_json or {}
        if (
            not started.is_set()
            and "org=example.com" in route.request.url
            and body.get("params", {}).get("name") == "inbox"
        ):
            response = await route.fetch()
            started.set()
            await release.wait()
            await route.fulfill(response=response)
            completed.set()
        else:
            await route.continue_()

    await page.route("**/mcp?**", delay_inbox)
    await page.locator("#refresh").click()
    await asyncio.wait_for(started.wait(), 5)
    try:
        await page.locator("#org").fill("other.example")
        await page.locator("#connect").click()
        await expect(page.locator("#messages")).to_contain_text("New organization message")
    finally:
        release.set()
    await asyncio.wait_for(completed.wait(), 5)
    await expect(page.locator("#messages")).not_to_contain_text("Old organization message")
    await expect(page.locator("#error-banner")).not_to_be_visible()


async def test_agent_guide_without_auth(browser: Browser, server: BaseTestServer) -> None:
    page = await browser.new_page()
    auth_requests: list[str] = []
    page.on(
        "request",
        lambda request: auth_requests.append(request.url) if "/admin/" in request.url else None,
    )
    await page.goto(str(server.make_url("/ui/?org=example.com&user=alice")))
    await expect(page.locator("#channels")).to_contain_text("general")
    await expect(page.locator("#login-dialog")).not_to_be_visible()
    await page.locator("#agent-open").click()
    await expect(page.locator("#agent-dialog")).to_be_visible()
    await expect(page.locator("#agent-user")).to_have_value("agent")
    await expect(page.locator("#agent-endpoint")).to_have_value(re.compile("user=agent"))
    await expect(page.locator("#agent-token")).not_to_be_visible()
    await expect(page.locator("#agent-auth")).not_to_be_visible()
    await expect(page.locator("#agent-issue")).not_to_be_visible()
    await expect(page.locator("#agent-ttl")).not_to_be_visible()
    await page.locator("#agent-user").fill("review-agent")
    for variant in ["codex-once", "codex-persistent", "claude-code"]:
        await page.locator("#agent-client").select_option(variant)
        code = await page.locator("#agent-code").input_value()
        assert "user=review-agent" in code
        assert "Authorization" not in code
        assert ("codex -c" in code) == (variant == "codex-once")
    for variant, key in [
        ("claude-project", "mcpServers"),
        ("pi", "mcpServers"),
        ("cursor", "mcpServers"),
        ("vscode", "servers"),
    ]:
        await page.locator("#agent-client").select_option(variant)
        config = json.loads(await page.locator("#agent-code").input_value())
        assert "user=review-agent" in config[key]["mcp_chat"]["url"]
    await expect(page.locator("#agent-prompt")).to_have_value(re.compile("chat://rules"))
    await page.keyboard.press("Escape")
    await expect(page.locator("#agent-dialog")).not_to_be_visible()
    assert not auth_requests


@pytest.mark.parametrize("width", [390, 1440])
@pytest.mark.parametrize("color_scheme", ["light", "dark"])
async def test_agent_form_layout(
    browser: Browser, tmp_path: Path, width: int, color_scheme: Literal["light", "dark"]
) -> None:
    secret = "browser-layout-secret-at-least-thirty-two-bytes"
    async with TestServer(create_app(tmp_path / "layout.sqlite3", jwt_secret=secret)) as server:
        page = await browser.new_page(viewport={"width": width, "height": 900})
        await page.emulate_media(color_scheme=color_scheme)
        await page.goto(str(server.make_url("/ui/?org=example.com&user=agent")))
        await page.locator("#login-secret").fill(secret)
        await page.locator("#login-submit").click()
        await expect(page.locator("#channels")).to_contain_text("general")
        await page.locator("#agent-open").click()
        await expect(page.locator("#agent-user")).to_have_value("chat-agent")
        await expect(page.locator("#agent-user")).to_be_focused()
        rows = await page.locator(".agent-settings").evaluate_all(
            "rows => rows.map(row => Array.from(row.querySelectorAll('input, select'), "
            "element => { const r = element.getBoundingClientRect(); "
            "return {x: r.x, y: r.y, width: r.width, height: r.height}; }))"
        )
        for left, right in rows:
            assert left["height"] == right["height"] == 40
            assert left["width"] == pytest.approx(right["width"], abs=1)
            if width > 650:
                assert left["y"] == right["y"]
                assert left["x"] + left["width"] < right["x"]
            else:
                assert left["x"] == right["x"]
                assert left["y"] + left["height"] < right["y"]
        assert await page.locator("#agent-dialog").evaluate(
            "dialog => dialog.scrollWidth <= dialog.clientWidth"
        )
        close = await page.locator("#agent-close").bounding_box()
        assert close and close["height"] == close["width"] == 36
        await page.locator("#agent-issue").scroll_into_view_if_needed()
        await expect(page.locator("#agent-issue")).to_be_in_viewport()
        await page.locator("#agent-close").click()
        await expect(page.locator("#agent-dialog")).not_to_be_visible()
        await page.close()


async def test_secret_login_and_agent_token(browser: Browser, tmp_path: Path) -> None:
    secret = "browser-sign-in-secret-at-least-thirty-two-bytes"
    async with TestServer(create_app(tmp_path / "protected.sqlite3", jwt_secret=secret)) as server:
        page = await browser.new_page()
        failures: list[str] = []
        page.on("pageerror", lambda error: failures.append(str(error)))
        await page.goto(str(server.make_url("/ui/?org=example.com&user=alice")))
        await expect(page.locator("#login-dialog")).to_be_visible()
        await expect(page.locator("#login-secret")).to_have_attribute("type", "password")
        await page.locator("#login-secret").fill("wrong-secret")
        await page.locator("#login-submit").click()
        await expect(page.locator("#login-error")).not_to_be_empty()
        await expect(page.locator("#login-dialog")).to_be_visible()
        await page.locator("#login-secret").fill(secret)
        await page.locator("#login-submit").click()
        await expect(page.locator("#channels")).to_contain_text("general")
        await expect(page.locator("#login-dialog")).not_to_be_visible()
        await expect(page.locator("#login-secret")).to_have_value("")
        assert secret not in await page.evaluate("JSON.stringify(localStorage)")
        assert secret not in await page.evaluate("JSON.stringify(sessionStorage)")
        cookies = await page.context.cookies()
        cookie = next(item for item in cookies if item["name"] == "mcp_chat_web")
        assert cookie["httpOnly"] and cookie["sameSite"] == "Strict"
        await page.reload()
        await expect(page.locator("#channels")).to_contain_text("general")
        await expect(page.locator("#login-dialog")).not_to_be_visible()
        await page.locator("#org").fill("other.example")
        await page.locator("#connect").click()
        await expect(page.locator("#workspace-name")).to_have_text("other.example")
        await page.locator("#agent-open").click()
        await expect(page.locator("#agent-user")).to_have_value("agent")
        await page.locator("#agent-user").fill("review-agent")
        await expect(page.locator("#agent-ttl")).to_have_value("30")
        await expect(page.locator("#agent-ttl-unit")).to_have_value("86400")
        await page.locator("#agent-issue").click()
        await expect(page.locator("#agent-token")).not_to_have_value("")
        token = await page.locator("#agent-token").input_value()
        claims = jwt.decode(token, secret, algorithms=["HS256"], audience="mcp-chat")
        assert claims["exp"] - claims["iat"] == 30 * 86400
        await expect(page.locator("#agent-expiry")).to_contain_text("30 days")
        await page.locator("#agent-client").select_option("claude-project")
        config = json.loads(await page.locator("#agent-code").input_value())["mcpServers"][
            "mcp_chat"
        ]
        assert config["headers"]["Authorization"] == f"Bearer {token}"
        assert urlsplit(config["url"]).query == ""
        async with aiohttp.ClientSession(headers={"Authorization": f"Bearer {token}"}) as session:
            async with Client(
                config["url"] + "?org=wrong&user=wrong", MODERN, session=session
            ) as client:
                assert await call(client, "identity") == {
                    "org": "other.example",
                    "user": "review-agent",
                }
        await page.locator("#agent-auth").select_option("url")
        url = await page.locator("#agent-endpoint").input_value()
        async with Client(url, MODERN) as client:
            assert await call(client, "identity") == {
                "org": "other.example",
                "user": "review-agent",
            }
        assert token not in await page.evaluate("JSON.stringify(localStorage)")
        await page.locator("#agent-ttl-unit").select_option("3600")
        await page.locator("#agent-ttl").fill("2")
        await expect(page.locator("#agent-token")).to_have_value("")
        async with page.expect_request("**/admin/token") as issued:
            await page.locator("#agent-issue").click()
        request_body = (await issued.value).post_data_json
        assert isinstance(request_body, dict)
        assert request_body["ttl_seconds"] == 7200
        await expect(page.locator("#agent-token")).not_to_have_value("")
        custom_token = await page.locator("#agent-token").input_value()
        claims = jwt.decode(custom_token, secret, algorithms=["HS256"], audience="mcp-chat")
        assert claims["exp"] - claims["iat"] == 7200
        await expect(page.locator("#agent-expiry")).to_contain_text("2 hours")
        await expect(page.locator("#agent-expiry")).to_contain_text("expires")
        requests: list[str] = []
        page.on("request", lambda request: requests.append(request.url))
        await page.locator("#agent-ttl").fill("721")
        await page.locator("#agent-issue").click()
        assert not await page.locator("#agent-ttl").evaluate("node => node.checkValidity()")
        assert not any(url.endswith("/admin/token") for url in requests)
        await expect(page.locator("#agent-token")).to_have_value("")
        assert not failures
        await page.context.close()


async def test_direct_messages_poll_reply_and_stay_private(
    browser: Browser, server: BaseTestServer
) -> None:
    alice = await open_chat(browser, server)
    bob = await open_chat(browser, server, "bob")
    carol = await open_chat(browser, server, "carol")
    failures: list[str] = []
    for page in (alice, bob, carol):
        page.on("pageerror", lambda error: failures.append(str(error)))
    await alice.locator("#dm-open").click()
    await alice.locator("#dm-user").fill("bob")
    await alice.locator("#dm-create").click()
    await expect(alice.locator("#view-title")).to_contain_text("bob")
    await expect(alice.locator("#subscribe-toggle")).not_to_be_visible()
    await alice.locator("#message-text").fill("Private release discussion")
    await alice.locator("#send-message").click()
    await expect(bob.locator("#messages")).to_contain_text(
        "Private release discussion", timeout=10000
    )
    await expect(bob.locator("#direct-messages")).to_contain_text("alice")
    await expect(bob.locator("#inbox-badge")).to_have_text("1")
    await expect(carol.locator("#direct-messages button[data-channel]")).to_have_count(0)
    await expect(carol.locator("#messages")).not_to_contain_text("Private release discussion")
    await expect(alice.locator("#channels")).not_to_contain_text("bob")
    message = bob.locator("#messages article[data-message-id]")
    await message.get_by_role("button", name="Reply in thread").click()
    await bob.locator("#reply-text").fill("Private response confirmed")
    await expect(bob.locator("#reply-send-label")).to_have_text("Also send to conversation")
    await bob.locator("#reply-send-to-channel").check()
    await bob.locator("#send-reply").click()
    await expect(bob.locator("#thread-messages")).to_contain_text("Private response confirmed")
    await expect(alice.locator("#messages")).to_contain_text(
        "Private response confirmed", timeout=10000
    )
    await bob.locator("#thread-close").click()
    await bob.locator("#mark-read").click()
    await expect(bob.locator("#inbox-badge")).to_have_text("0")
    await bob.locator("#direct-messages button[data-channel]").click()
    await expect(bob.locator("#view-title")).to_contain_text("alice")
    await expect(bob.locator("#messages")).to_contain_text("Private release discussion")
    await bob.reload()
    await expect(bob.locator("#view-title")).to_contain_text("alice")
    await expect(bob.locator("#messages")).to_contain_text("Private response confirmed")
    await bob.set_viewport_size({"width": 390, "height": 844})
    assert await bob.evaluate("document.documentElement.scrollWidth <= innerWidth")
    for page in (alice, carol):
        await page.locator("#search-open").click()
        await page.locator("#search-query").fill("Private")
        await page.locator("#search-form button").click()
    await expect(alice.locator("#messages")).to_contain_text("Private release discussion")
    await expect(carol.locator("#messages")).to_contain_text("No messages found")
    await alice.locator("#org").fill("other.example")
    await alice.locator("#connect").click()
    await expect(alice.locator("#channels")).to_contain_text("general")
    await expect(alice.locator("#direct-messages button[data-channel]")).to_have_count(0)
    assert not failures


async def test_mention_inbox_can_be_acknowledged_without_joining(
    browser: Browser, server: BaseTestServer
) -> None:
    bob = await open_chat(browser, server, "bob")
    failures: list[str] = []
    bob.on("pageerror", lambda error: failures.append(str(error)))
    async with Client(str(server.make_url("/mcp?org=example.com&user=alice")), MODERN) as alice:
        await call(alice, "subscribe", channel="random")
        mention = await call(
            alice, "post_message", channel="random", text="@bob, review without joining"
        )
        await expect(bob.locator("#messages")).to_contain_text(
            "@bob, review without joining", timeout=10000
        )
        await expect(bob.locator("#inbox-badge")).to_have_text("1")
        row = bob.locator(f'#messages [data-message-id="{mention["id"]}"]')
        await row.get_by_role("button", name="Mark read", exact=True).click()
        await expect(bob.locator("#inbox-badge")).to_have_text("0")
        await expect(bob.locator("#messages")).not_to_contain_text("review without joining")
        await channel(bob, "random")
        await expect(bob.locator("#subscribe-toggle")).to_have_text("Join channel")
    async with Client(str(server.make_url("/mcp?org=example.com&user=bob")), MODERN) as client:
        assert (await call(client, "list_subscriptions"))["subscriptions"] == []
        assert (await call(client, "inbox"))["messages"] == []
    assert not failures


async def test_long_thread_keeps_desktop_panes_and_responsive_overlay_within_viewport(
    browser: Browser, server: BaseTestServer
) -> None:
    async with Client(str(server.make_url("/mcp?org=example.com&user=alice")), MODERN) as client:
        await call(client, "create_channel", name="long-thread")
        root = await call(
            client,
            "post_message",
            channel="long-thread",
            text="Long root\n" + "A full line of discussion.\n" * 80 + "x" * 700,
        )
        for index in range(8):
            await call(
                client,
                "post_message",
                channel="long-thread",
                text=f"Long reply {index}\n" + "Further discussion.\n" * 12,
                parent_id=root["id"],
            )
    page = await open_chat(browser, server)
    await page.set_viewport_size({"width": 1440, "height": 1000})
    await channel(page, "long-thread")
    root_row = page.locator(f'#messages [data-message-id="{root["id"]}"]')
    await root_row.get_by_role("button", name="View thread (8 replies)", exact=True).click()
    await expect(page.locator("#thread-messages .message")).to_have_count(9)
    await page.evaluate("window.scrollTo(0, 0)")
    dimensions = await page.evaluate("""() => ({
        height: Math.max(document.body.scrollHeight, document.documentElement.scrollHeight),
        width: document.documentElement.scrollWidth,
        viewportHeight: window.innerHeight,
        viewportWidth: window.innerWidth,
    })""")
    assert dimensions["height"] <= dimensions["viewportHeight"] + 1
    assert dimensions["width"] <= dimensions["viewportWidth"]
    for selector in (
        ".topbar",
        ".panel-heading",
        "#composer",
        ".thread-heading",
        "#thread-composer",
        "#thread-close",
    ):
        await expect(page.locator(selector)).to_be_in_viewport(ratio=1)
    for selector in ("#messages", "#thread-messages"):
        assert await page.locator(selector).evaluate(
            "node => node.scrollHeight > node.clientHeight"
        )
    await page.locator("#messages").evaluate("node => { node.scrollTop = 150; }")
    await page.locator("#thread-messages").evaluate("node => { node.scrollTop = 0; }")
    main_scroll = await page.locator("#messages").evaluate("node => node.scrollTop")
    topbar_top = await page.locator(".topbar").evaluate("node => node.getBoundingClientRect().top")
    await page.locator("#thread-messages").hover()
    await page.mouse.wheel(0, 600)
    await page.wait_for_function("document.querySelector('#thread-messages').scrollTop > 0")
    assert await page.locator("#messages").evaluate("node => node.scrollTop") == main_scroll
    assert (
        await page.locator(".topbar").evaluate("node => node.getBoundingClientRect().top")
        == topbar_top
    )
    assert await page.evaluate("window.scrollY") == 0
    await expect(page.locator("#thread-composer")).to_be_in_viewport(ratio=1)
    for viewport in ({"width": 900, "height": 700}, {"width": 390, "height": 844}):
        await page.set_viewport_size({"width": viewport["width"], "height": viewport["height"]})
        for selector in (".thread-heading", "#thread-composer", "#thread-close"):
            await expect(page.locator(selector)).to_be_in_viewport(ratio=1)
        box = await page.locator("#thread-panel").bounding_box()
        assert box is not None
        assert box["y"] >= 0 and box["y"] + box["height"] <= viewport["height"] + 1
        assert box["x"] >= 0 and box["x"] + box["width"] <= viewport["width"] + 1
        assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        await page.locator("#thread-close").click()
        await expect(page.locator("#thread-panel")).not_to_be_visible()
        await root_row.get_by_role("button", name="View thread (8 replies)", exact=True).click()
        await expect(page.locator("#thread-close")).to_be_in_viewport(ratio=1)
        await expect(page.locator("#thread-composer")).to_be_in_viewport(ratio=1)
    await page.set_viewport_size({"width": 1440, "height": 1000})
    await page.locator("#thread-close").click()
    await expect(page.locator("#thread-panel")).not_to_be_visible()
    assert await page.evaluate("document.documentElement.scrollHeight <= innerHeight + 1")
    await expect(page.locator("#composer")).to_be_in_viewport(ratio=1)


async def test_thread_replies_stay_in_thread_and_can_be_broadcast(
    browser: Browser, server: BaseTestServer
) -> None:
    async with (
        Client(str(server.make_url("/mcp?org=example.com&user=alice")), MODERN) as alice,
        Client(str(server.make_url("/mcp?org=example.com&user=bob")), MODERN) as bob,
        Client(str(server.make_url("/mcp?org=example.com&user=carol")), MODERN) as carol,
    ):
        await call(alice, "create_channel", name="team")
        await call(bob, "subscribe", channel="team")
        await call(carol, "subscribe", channel="team")
        root = await call(alice, "post_message", channel="team", text="First topic")
        other = await call(alice, "post_message", channel="team", text="Second topic")
        await call(carol, "mark_read", channel="team", message_ids=[root["id"], other["id"]])
        page = await open_chat(browser, server, "bob")
        await channel(page, "team")
        row = page.locator(f'#messages [data-message-id="{root["id"]}"]')
        await row.get_by_role("button", name="Reply in thread", exact=True).click()
        await expect(page.locator("#reply-send-to-channel")).not_to_be_checked()
        assert len((await call(bob, "inbox"))["messages"]) == 2
        await page.locator("#reply-text").fill("Thread-only response")
        await page.locator("#send-reply").click()
        await expect(page.locator("#thread-messages")).to_contain_text("Thread-only response")
        await expect(row.get_by_role("button", name="View thread (1 reply)")).to_be_visible()
        await expect(page.locator("#messages")).not_to_contain_text("Thread-only response")
        assert [message["text"] for message in (await call(alice, "inbox"))["messages"]] == [
            "Thread-only response"
        ]
        assert (await call(carol, "inbox"))["messages"] == []
        await page.locator("#thread-mark-read").click()
        await expect(page.locator("#inbox-badge")).to_have_text("1")
        assert [message["id"] for message in (await call(bob, "inbox"))["messages"]] == [
            other["id"]
        ]
        await page.locator("#reply-send-to-channel").check()
        await page.locator("#reply-text").fill("Broadcast response")
        await page.locator("#send-reply").click()
        await expect(page.locator("#messages")).to_contain_text("Broadcast response")
        await expect(page.locator("#thread-messages")).to_contain_text("Broadcast response")
        await expect(page.locator("#reply-send-to-channel")).not_to_be_checked()
        history = (await call(bob, "history", channel="team"))["messages"]
        thread = (await call(bob, "thread", message_id=root["id"]))["messages"]
        broadcast = next(message for message in history if message["text"] == "Broadcast response")
        assert broadcast["send_to_channel"] is True
        assert [message["id"] for message in thread if message["text"] == "Broadcast response"] == [
            broadcast["id"]
        ]
        assert [message["id"] for message in (await call(carol, "inbox"))["messages"]] == [
            broadcast["id"]
        ]
        await page.locator("#reply-send-to-channel").check()
        await page.locator("#thread-close").click()
        await row.get_by_role("button", name="View thread (2 replies)", exact=True).click()
        await expect(page.locator("#reply-send-to-channel")).not_to_be_checked()
        assert [message["id"] for message in (await call(bob, "inbox"))["messages"]] == [
            other["id"]
        ]


async def test_live_reply_counts_preserve_loaded_history_scroll_and_draft(
    browser: Browser, server: BaseTestServer
) -> None:
    async with Client(str(server.make_url("/mcp?org=example.com&user=alice")), MODERN) as client:
        await call(client, "create_channel", name="history")
        roots = []
        for index in range(105):
            roots.append(
                await call(client, "post_message", channel="history", text=f"Topic {index}")
            )
        page = await open_chat(browser, server)
        await channel(page, "history")
        await expect(page.locator("#messages .message")).to_have_count(50)
        await page.locator("#load-more").click()
        await expect(page.locator("#messages .message")).to_have_count(100)
        await page.locator("#load-more").click()
        await expect(page.locator("#messages .message")).to_have_count(105)
        await page.locator("#message-text").fill("Unsent channel draft")
        await page.locator("#messages").evaluate("node => { node.scrollTop = 250; }")
        original_scroll = await page.locator("#messages").evaluate("node => node.scrollTop")
        await call(
            client,
            "post_message",
            channel="history",
            text="Only in the thread",
            parent_id=roots[0]["id"],
        )
        first = page.locator(f'#messages [data-message-id="{roots[0]["id"]}"]')
        await expect(first.get_by_role("button", name="View thread (1 reply)")).to_be_attached()
        await expect(page.locator("#messages .message")).to_have_count(105)
        await expect(page.locator("#messages")).not_to_contain_text("Only in the thread")
        await expect(page.locator("#message-text")).to_have_value("Unsent channel draft")
        assert await page.locator("#messages").evaluate("node => node.scrollTop") == original_scroll
        await expect(page.locator("#load-more")).not_to_be_visible()
        acknowledgements: list[dict[str, Any]] = []

        def record_acknowledgement(request: Any) -> None:
            if request.method == "POST":
                payload = request.post_data_json
                if payload.get("params", {}).get("name") == "mark_read":
                    acknowledgements.append(payload["params"]["arguments"])

        page.on("request", record_acknowledgement)

        def final_acknowledgement(response: Any) -> bool:
            if response.request.method != "POST":
                return False
            payload = response.request.post_data_json or {}
            return (
                payload.get("params", {}).get("name") == "mark_read"
                and len(payload["params"]["arguments"]["message_ids"]) == 5
            )

        async with page.expect_response(final_acknowledgement) as final_batch:
            await page.locator("#mark-read").click()
        await (await final_batch.value).finished()
        assert [len(batch["message_ids"]) for batch in acknowledgements] == [100, 5]
        assert [
            message_id for batch in acknowledgements for message_id in batch["message_ids"]
        ] == [root["id"] for root in roots]
        assert all(batch["channel"] == "history" for batch in acknowledgements)


@pytest.mark.parametrize("initial_count", [49, 50, 100])
@pytest.mark.parametrize("sender", ["alice", "bob"])
async def test_channel_at_end_keeps_live_messages_across_page_boundaries(
    browser: Browser, server: BaseTestServer, initial_count: int, sender: str
) -> None:
    async with (
        Client(str(server.make_url("/mcp?org=example.com&user=alice")), MODERN) as alice,
        Client(str(server.make_url("/mcp?org=example.com&user=bob")), MODERN) as bob,
    ):
        await call(alice, "create_channel", name="boundary")
        await call(bob, "subscribe", channel="boundary")
        for index in range(initial_count):
            await call(alice, "post_message", channel="boundary", text=f"Initial {index}")
        page = await open_chat(browser, server)
        await channel(page, "boundary")
        await expect(page.locator("#messages .message")).to_have_count(min(initial_count, 50))
        if initial_count > 50:
            await page.locator("#load-more").click()
            await expect(page.locator("#messages .message")).to_have_count(initial_count)
        await expect(page.locator("#load-more")).not_to_be_visible()
        for index in range(3):
            text = f"New message from {sender}: {index}"
            if sender == "alice":
                await page.locator("#message-text").fill(text)
                await page.locator("#send-message").click()
                await expect(page.locator("#message-text")).to_have_value("")
            else:
                await call(bob, "post_message", channel="boundary", text=text)
            await expect(page.locator("#messages")).to_contain_text(text)
            await expect(page.locator("#messages .message")).to_have_count(
                initial_count + index + 1
            )
        await expect(page.locator("#load-more")).not_to_be_visible()


@pytest.mark.parametrize("existing_replies", [0, 55])
async def test_reply_notification_opens_and_reveals_the_notified_message(
    browser: Browser, server: BaseTestServer, existing_replies: int
) -> None:
    async with (
        Client(str(server.make_url("/mcp?org=example.com&user=alice")), MODERN) as alice,
        Client(str(server.make_url("/mcp?org=example.com&user=bob")), MODERN) as bob,
    ):
        await call(bob, "create_channel", name="notification-thread")
        await call(alice, "subscribe", channel="notification-thread")
        root = await call(bob, "post_message", channel="notification-thread", text="Root topic")
        for index in range(existing_replies):
            await call(
                alice,
                "post_message",
                channel="notification-thread",
                parent_id=root["id"],
                text=f"Earlier reply {index}",
            )
        page = await browser.new_page(viewport={"width": 1440, "height": 900})
        await page.add_init_script("""
            window.alerts = [];
            window.Notification = class {
              static permission = 'granted';
              constructor(title, options) {
                Object.assign(this, {title}, options);
                window.alerts.push(this);
              }
              close() {}
            };
            document.hasFocus = () => false;
        """)
        await page.goto(str(server.make_url("/ui/?org=example.com&user=bob")))
        await expect(page.locator("#sync-note")).to_have_text("Live MCP updates")
        reply = await call(
            alice,
            "post_message",
            channel="notification-thread",
            parent_id=root["id"],
            text="Reply requiring attention",
        )
        await page.wait_for_function(
            "alerts.some(alert => alert.body === 'Reply requiring attention')"
        )
        await page.evaluate(
            "alerts.find(alert => alert.body === 'Reply requiring attention').onclick()"
        )
        await expect(page.locator("#view-title")).to_contain_text("notification-thread")
        await expect(page.locator("#thread-panel")).to_be_visible()
        target = page.locator(f'#thread-messages [data-message-id="{reply["id"]}"]')
        await expect(target).to_contain_text("Reply requiring attention")
        await expect(target).to_be_in_viewport()
        await expect(page.locator("#messages")).not_to_contain_text("Reply requiring attention")


async def test_history_refresh_preserves_pagination_and_ignores_previous_view(
    browser: Browser, server: BaseTestServer
) -> None:
    async with Client(str(server.make_url("/mcp?org=example.com&user=alice")), MODERN) as client:
        await call(client, "create_channel", name="paged")
        roots = []
        for index in range(60):
            roots.append(await call(client, "post_message", channel="paged", text=f"Topic {index}"))
        await call(client, "create_channel", name="other")
        await call(client, "post_message", channel="other", text="Other conversation")
        page = await open_chat(browser, server)
        await channel(page, "paged")
        await expect(page.locator("#messages .message")).to_have_count(50)
        await call(client, "post_message", channel="paged", parent_id=roots[0]["id"], text="Reply")
        await expect(page.get_by_role("button", name="View thread (1 reply)")).to_be_visible()
        await expect(page.locator("#messages .message")).to_have_count(50)
        await expect(page.locator("#load-more")).to_be_visible()
        waiting = asyncio.Event()
        release = asyncio.Event()

        async def delay_history(route: Route) -> None:
            payload = route.request.post_data_json if route.request.method == "POST" else None
            params = (payload or {}).get("params", {})
            if (
                params.get("name") == "history"
                and params.get("arguments", {}).get("channel") == "paged"
            ):
                waiting.set()
                await release.wait()
            await route.continue_()

        await page.route("**/mcp**", delay_history)
        try:
            await page.locator("#refresh").click()
            await asyncio.wait_for(waiting.wait(), timeout=5)
            await channel(page, "other")
        finally:
            release.set()
        await expect(page.locator("#messages")).to_contain_text("Other conversation")
        await expect(page.locator("#messages .message")).to_have_count(1)
        await expect(page.locator("#messages")).not_to_contain_text("Topic")
        await expect(page.locator("#load-more")).not_to_be_visible()
