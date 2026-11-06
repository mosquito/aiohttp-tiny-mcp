"""MCP tools and HTTP application lifecycle."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from aiohttp import web
from aiohttp_tiny_mcp import Endpoint, Principal, Registry
from aiohttp_tiny_mcp.console import Console
from aiohttp_tiny_mcp.storage.sqlite import SqliteHub, SqliteSessionStore, SqliteStorage

from .auth import DemoIdentity, JWTIdentity, setup_auth
from .models import (
    Channel,
    CreateChannel,
    DirectMessage,
    DirectUser,
    Empty,
    History,
    MarkRead,
    Page,
    PostMessage,
    Search,
    Thread,
)
from .notifications import NotificationHub, NotificationIdentity
from .observe import ObservedDispatcher
from .observe import setup as setup_observe
from .rules import RULES, TOOL_HELP
from .store import Store
from .webui import setup as setup_webui

READ_ONLY = {"readOnlyHint": True}
IDEMPOTENT = {"destructiveHint": False, "idempotentHint": True}
APPEND = {"destructiveHint": False, "idempotentHint": False}


def organization(principal: Principal) -> str:
    assert principal.namespace is not None
    return principal.namespace


def username(principal: Principal) -> str:
    assert principal.subject is not None
    return principal.subject


def message_page(messages: list[dict[str, Any]], after: int) -> dict[str, Any]:
    return {"messages": messages, "next_after": messages[-1]["id"] if messages else after}


def build_registry(
    store: Store, storage: SqliteStorage, *, jwt_secret: str | None = None
) -> Registry:
    notifications = NotificationHub(SqliteHub(storage))
    registry = Registry(
        "mcp-chat",
        "0.1.0",
        auth=NotificationIdentity(
            DemoIdentity(store) if jwt_secret is None else JWTIdentity(store, jwt_secret)
        ),
        hub=notifications,
        session_store=SqliteSessionStore(storage),
        instructions=RULES,
    )

    async def membership_updated(principal: Principal) -> None:
        for uri in ("chat://channels", "chat://subscriptions", "chat://inbox"):
            await notifications.updated(uri, users=[username(principal)])

    async def message_updated(principal: Principal, message: dict[str, Any]) -> None:
        audience = await store.notification_recipients(
            organization(principal), username(principal), message["channel"]
        )
        private = audience["kind"] == "dm"
        recipients = audience["users"] if private else None
        await notifications.updated(f"chat://channels/{message['channel']}", users=recipients)
        root = message["parent_id"] or message["id"]
        await notifications.updated(f"chat://threads/{root}", users=recipients)
        if private:
            await notifications.updated("chat://dms", users=recipients)
        else:
            await notifications.updated("chat://channels")
            await notifications.updated("chat://subscriptions", users=audience["users"])
        unread = [user for user in audience["users"] if user != username(principal)]
        await notifications.updated("chat://inbox", users=unread)

    @registry.tool(annotations=READ_ONLY, description=TOOL_HELP["identity"])
    async def identity(args: Empty, principal: Principal) -> dict:
        """Return the organization and user selected by this connection."""
        return {"org": organization(principal), "user": username(principal)}

    @registry.tool(annotations=APPEND, description=TOOL_HELP["create_channel"])
    async def create_channel(args: CreateChannel, principal: Principal) -> dict:
        """Create a public channel and subscribe its creator."""
        result = await store.create_channel(
            organization(principal), username(principal), args.name, args.description
        )
        await notifications.updated("chat://channels")
        await notifications.updated("chat://subscriptions", users=[username(principal)])
        return result

    @registry.tool(annotations=READ_ONLY, description=TOOL_HELP["list_channels"])
    async def list_channels(args: Empty, principal: Principal) -> dict:
        """List this organization's channels with your subscription and unread counts."""
        return {"channels": await store.list_channels(organization(principal), username(principal))}

    @registry.tool(annotations=IDEMPOTENT, description=TOOL_HELP["subscribe"])
    async def subscribe(args: Channel, principal: Principal) -> dict:
        """Subscribe to future messages; an existing subscription keeps its read position."""
        result = await store.subscribe(organization(principal), username(principal), args.channel)
        await membership_updated(principal)
        return result

    @registry.tool(annotations=IDEMPOTENT, description=TOOL_HELP["unsubscribe"])
    async def unsubscribe(args: Channel, principal: Principal) -> dict:
        """Leave a channel and remove its unread messages from your inbox."""
        result = await store.unsubscribe(organization(principal), username(principal), args.channel)
        await membership_updated(principal)
        return result

    @registry.tool(annotations=READ_ONLY, description=TOOL_HELP["list_subscriptions"])
    async def list_subscriptions(args: Empty, principal: Principal) -> dict:
        """List your subscribed channels and their unread counts."""
        return {
            "subscriptions": await store.list_subscriptions(
                organization(principal), username(principal)
            )
        }

    @registry.tool(annotations=IDEMPOTENT, description=TOOL_HELP["open_dm"])
    async def open_dm(args: DirectUser, principal: Principal) -> dict:
        """Open your private conversation with another user in this organization."""
        result = await store.open_dm(organization(principal), username(principal), args.user)
        await notifications.updated("chat://dms", users=[username(principal), args.user])
        return result

    @registry.tool(annotations=READ_ONLY, description=TOOL_HELP["list_dms"])
    async def list_dms(args: Empty, principal: Principal) -> dict:
        """List only your direct conversations, peers, and unread counts."""
        return {"conversations": await store.list_dms(organization(principal), username(principal))}

    @registry.tool(annotations=APPEND, description=TOOL_HELP["send_dm"])
    async def send_dm(args: DirectMessage, principal: Principal) -> dict:
        """Send a private message or thread reply to another user."""
        result = await store.send_dm(
            organization(principal), username(principal), args.user, args.text, args.parent_id
        )
        await message_updated(principal, result)
        return result

    @registry.tool(annotations=APPEND, description=TOOL_HELP["post_message"])
    async def post_message(args: PostMessage, principal: Principal) -> dict:
        """Post to a subscribed channel; set parent_id to reply to a root message."""
        result = await store.post_message(
            organization(principal), username(principal), args.channel, args.text, args.parent_id
        )
        await message_updated(principal, result)
        return result

    @registry.tool(annotations=READ_ONLY, description=TOOL_HELP["history"])
    async def history(args: History, principal: Principal) -> dict:
        """Read channel messages and replies in ascending ID order without marking them read."""
        return message_page(
            await store.history(
                organization(principal),
                args.channel,
                args.after,
                args.limit,
                user=username(principal),
            ),
            args.after,
        )

    @registry.tool(annotations=READ_ONLY, description=TOOL_HELP["thread"])
    async def thread(args: Thread, principal: Principal) -> dict:
        """Read a root message and its replies in ascending ID order."""
        return message_page(
            await store.thread(
                organization(principal),
                args.message_id,
                args.after,
                args.limit,
                user=username(principal),
            ),
            args.after,
        )

    @registry.tool(annotations=READ_ONLY, description=TOOL_HELP["inbox"])
    async def inbox(args: Page, principal: Principal) -> dict:
        """Read unread messages from subscribed channels, excluding your own messages."""
        return message_page(
            await store.inbox(organization(principal), username(principal), args.after, args.limit),
            args.after,
        )

    @registry.tool(annotations=IDEMPOTENT, description=TOOL_HELP["mark_read"])
    async def mark_read(args: MarkRead, principal: Principal) -> dict:
        """Mark a channel read through message_id; never move the read position backwards."""
        result = await store.mark_read(
            organization(principal), username(principal), args.channel, args.message_id
        )
        if await store.is_direct(organization(principal), username(principal), args.channel):
            await notifications.updated("chat://dms", users=[username(principal)])
            await notifications.updated("chat://inbox", users=[username(principal)])
        else:
            await membership_updated(principal)
        return result

    @registry.tool(annotations=READ_ONLY, description=TOOL_HELP["search"])
    async def search(args: Search, principal: Principal) -> dict:
        """Search this organization's message text with FTS5, optionally in one channel."""
        return message_page(
            await store.search(
                organization(principal),
                args.query,
                args.channel,
                args.after,
                args.limit,
                user=username(principal),
            ),
            args.after,
        )

    @registry.resource("chat://channels", mime_type="application/json")
    async def channels_resource(args: Empty, principal: Principal) -> dict:
        """List channels in the current organization."""
        return await list_channels(args, principal)

    @registry.resource("chat://channels/{channel}", mime_type="application/json")
    async def channel_resource(args: Channel, principal: Principal) -> dict:
        """Read the first 50 channel messages; use history for subsequent pages."""
        return await history(History(channel=args.channel), principal)

    @registry.resource("chat://channels/{channel}/messages/{after}", mime_type="application/json")
    async def channel_page_resource(args: History, principal: Principal) -> dict:
        """Read 50 channel messages after the supplied ID; continue using next_after."""
        return await history(args, principal)

    @registry.resource("chat://threads/{message_id}", mime_type="application/json")
    async def thread_resource(args: Thread, principal: Principal) -> dict:
        """Read a root and its replies; use thread or the messages template for another page."""
        return await thread(args, principal)

    @registry.resource("chat://threads/{message_id}/messages/{after}", mime_type="application/json")
    async def thread_page_resource(args: Thread, principal: Principal) -> dict:
        """Read 50 thread messages after the supplied ID; continue using next_after."""
        return await thread(args, principal)

    @registry.resource("chat://inbox", mime_type="application/json")
    async def inbox_resource(args: Empty, principal: Principal) -> dict:
        """Read your first 50 unread messages; use inbox with next_after for another page."""
        return await inbox(Page(), principal)

    @registry.resource("chat://subscriptions", mime_type="application/json")
    async def subscriptions_resource(args: Empty, principal: Principal) -> dict:
        """List your channel subscriptions and unread counts without changing them."""
        return await list_subscriptions(args, principal)

    @registry.resource("chat://dms", mime_type="application/json")
    async def dms_resource(args: Empty, principal: Principal) -> dict:
        """List your private conversations; read a returned channel ID through channel resources."""
        return await list_dms(args, principal)

    @registry.resource("chat://rules", mime_type="text/markdown")
    async def rules_resource(args: Empty) -> str:
        """Read communication rules, channel purposes, resources, and tool call examples."""
        return RULES

    return registry


def create_app(
    path: str | Path = "mcp-chat.sqlite3", *, jwt_secret: str | None = None
) -> web.Application:
    store = Store(path)
    storage = SqliteStorage(path)
    registry = build_registry(store, storage, jwt_secret=jwt_secret)
    endpoint = Endpoint(registry)
    dispatcher = ObservedDispatcher(registry, store)
    endpoint.dispatcher = dispatcher
    app = endpoint.app("/mcp")
    setup_webui(app, auth_required=bool(jwt_secret))
    setup_auth(app, jwt_secret)
    setup_observe(app, store, jwt_secret, dispatcher.feed)
    Console(
        "/mcp?org=example.com&user=human",
        title="MCP Chat",
        description="Select an organization and user in the endpoint URL to start chatting.",
    ).setup(app, "/console")

    async def chat_lifetime(app: web.Application) -> AsyncIterator[None]:
        await store.open()
        try:
            yield
        finally:
            await store.close()

    app.cleanup_ctx.extend([chat_lifetime, storage.cleanup_ctx])
    return app
