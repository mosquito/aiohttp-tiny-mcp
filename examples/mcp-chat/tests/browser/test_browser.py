from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import aiohttp
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
    await expect(page.locator("#agent-token")).not_to_be_visible()
    await expect(page.locator("#agent-auth")).not_to_be_visible()
    await expect(page.locator("#agent-issue")).not_to_be_visible()
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
        await page.locator("#agent-user").fill("review-agent")
        await page.locator("#agent-issue").click()
        await expect(page.locator("#agent-token")).not_to_have_value("")
        token = await page.locator("#agent-token").input_value()
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
