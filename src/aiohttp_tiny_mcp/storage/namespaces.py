"""Request namespaces isolate sessions, subscriptions, and pending questions.

Set namespace in authenticated middleware; spawned tasks inherit it:

    @web.middleware
    async def tenant(request, handler):
        namespace.set(request["user"].org_id)
        return await handler(request)

An unset namespace leaves keys bare for single-tenant deployments.
"""

from __future__ import annotations

from contextvars import ContextVar
from urllib.parse import quote

namespace: ContextVar[str | None] = ContextVar("aiohttp_tiny_mcp_namespace", default=None)

SEPARATOR = ":"


def current() -> str | None:
    """The namespace in force, or `None` where none was set."""
    return namespace.get()


def scoped(key: str) -> str:
    """Prefix a key with the percent-encoded namespace to prevent separator collisions."""
    name = namespace.get()
    if not name:
        return key
    return f"{quote(name, safe='')}{SEPARATOR}{key}"


__all__ = ["SEPARATOR", "current", "namespace", "scoped"]
