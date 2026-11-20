from __future__ import annotations

import time

from aiohttp.test_utils import TestClient
from cryptography.hazmat.primitives import serialization
from pydantic import BaseModel

from aiohttp_tiny_mcp import (
    Authorization,
    MemoryHub,
    MemorySessionStore,
    Principal,
    Registry,
    StaticVerifier,
)
from aiohttp_tiny_mcp.protocol.selection import AdapterSet
from aiohttp_tiny_mcp.storage.namespaces import current
from aiohttp_tiny_mcp.storage.sessions import SESSION_HEADER

MODERN = AdapterSet.default().by_version["2026-07-28"]


LEGACY = AdapterSet.default().by_version["2025-11-25"]


RESOURCE = "https://mcp.example.com/reports/mcp"


TOKENS = {
    "alice": Principal(
        subject="alice",
        client_id="reports",
        issuer="https://login.example.com",
        scopes=frozenset({"reports:read"}),
    ),
    "bob": Principal(
        subject="bob",
        client_id="reports",
        issuer="https://login.example.com",
        scopes=frozenset({"reports:read"}),
    ),
    "unscoped": Principal(
        subject="carol",
        client_id="reports",
        issuer="https://login.example.com",
        scopes=frozenset(),
    ),
    "stale": Principal(
        subject="dave",
        client_id="reports",
        issuer="https://login.example.com",
        scopes=frozenset({"reports:read"}),
        expires_at=time.time() - 60,
    ),
}


class Nothing(BaseModel):
    pass


def build(**options) -> tuple[Registry, StaticVerifier]:
    verifier = StaticVerifier(TOKENS)
    auth = Authorization(
        verifier=verifier,
        resource=RESOURCE,
        authorization_servers=["https://login.example.com"],
        scopes_supported=["reports:read"],
        **options,
    )
    registry = Registry(
        "reports", "1.0", hub=MemoryHub(), session_store=MemorySessionStore(), auth=auth
    )

    async def report(args: Nothing, principal: Principal) -> str:
        """Return the caller's report."""
        return f"report for {principal.subject}"

    async def public(args: Nothing) -> str:
        """Anyone holding a token may call this."""
        return "open"

    registry.tool(report, scopes=["reports:read"])
    registry.tool(public)
    return registry, verifier


HEADERS = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}


async def opened(client: TestClient, token: str) -> str:
    """Shake hands on a revision that keeps a session, and keep its id."""
    answered = await client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "t", "version": "0"},
            },
        },
        headers={**HEADERS, "Authorization": f"Bearer {token}"},
    )
    assert answered.status == 200
    return answered.headers[SESSION_HEADER]


def registry_for(auth):
    registry = Registry("protected", "1", auth=auth)

    @registry.tool(scopes=["read"])
    async def who(args: Nothing, principal: Principal) -> str:
        return f"{principal.subject}:{current()}"

    return registry


PSK = "test-pre-shared-key-with-at-least-32-bytes"


def pem(key):
    return (
        key.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )
