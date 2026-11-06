"""Standard MCP resource notifications with private recipient filtering."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from contextvars import ContextVar
from typing import Any

from aiohttp import web
from aiohttp_tiny_mcp import Authentication, Principal, Unauthorized
from aiohttp_tiny_mcp.storage.hub import NOTIFICATIONS, Cursor, Event, Hub, topic

ACTOR: ContextVar[tuple[str, str] | None] = ContextVar("mcp_chat_notification_actor", default=None)
RECIPIENTS = "mcp-chat/recipients"


class NotificationIdentity(Authentication):
    """Bind delivery to the authenticated actor, including background SSE tasks."""

    def __init__(self, policy: Authentication) -> None:
        super().__init__(
            bind_sessions=policy.bind_sessions,
            namespace_from_token=policy.namespace_from_token,
            required_scopes=policy.required_scopes,
        )
        self.policy = policy

    async def authenticate(self, request: web.Request) -> Principal:
        ACTOR.set(None)
        principal = self.policy.check(await self.policy.authenticate(request))
        assert principal.namespace is not None and principal.subject is not None
        ACTOR.set((principal.namespace, principal.subject))
        return principal

    def challenge(self, refusal: Unauthorized) -> str:
        return self.policy.challenge(refusal)


class NotificationHub(Hub):
    """Filter private events before MCP resource subscription matching."""

    def __init__(self, backend: Hub) -> None:
        self.backend = backend

    async def updated(self, uri: str, *, users: Sequence[str] | None = None) -> Cursor:
        message: dict[str, Any] = {
            "jsonrpc": "2.0",
            "method": "notifications/resources/updated",
            "params": {"uri": uri},
        }
        if users is not None:
            actor = ACTOR.get()
            if actor is None:
                raise RuntimeError("Private notifications require an authenticated actor.")
            message[RECIPIENTS] = {"org": actor[0], "users": list(dict.fromkeys(users))}
        return await self.publish(topic(NOTIFICATIONS), message)

    async def publish(self, topic: str, message: Mapping[str, Any]) -> Cursor:
        return await self.backend.publish(topic, message)

    async def position(self, topic: str) -> Cursor:
        return await self.backend.position(topic)

    async def poll(self, topic: str, cursor: Cursor, *, timeout: float) -> Sequence[Event]:
        events = await self.backend.poll(topic, cursor, timeout=timeout)
        actor = ACTOR.get()
        delivered = []
        for event in events:
            audience = event.message.get(RECIPIENTS)
            if RECIPIENTS not in event.message:
                delivered.append(event)
                continue
            allowed = (
                actor is not None
                and isinstance(audience, Mapping)
                and audience.get("org") == actor[0]
                and isinstance(audience.get("users"), list)
                and actor[1] in audience["users"]
            )
            message = (
                {key: value for key, value in event.message.items() if key != RECIPIENTS}
                if allowed
                else {}
            )
            delivered.append(Event(event.id, message))
        return delivered

    async def delete(self, topic: str) -> None:
        await self.backend.delete(topic)
