"""Opaque OAuth tokens share storage while isolating identities and resources."""

import time
from dataclasses import replace

import pytest

from aiohttp_tiny_mcp import MemorySessionStore, Principal
from aiohttp_tiny_mcp.oauth import OpaqueTokens


async def test_opaque_tokens_share_storage_and_revoke_independently():
    store = MemorySessionStore()
    config = {"issuer": "https://mcp.test/oauth", "resource": "https://mcp.test/mcp"}
    first = OpaqueTokens(store=store, **config)
    worker = OpaqueTokens(store=store, **config)
    alice = Principal(
        subject="alice",
        issuer=config["issuer"],
        expires_at=time.time() + 60,
        scopes=frozenset({"read"}),
        namespace="team",
    )
    bob = replace(alice, subject="bob")
    alice_token = await first.issue(alice, {"access_token": "provider-secret"})
    bob_token = await first.issue(bob, {})
    assert alice_token != bob_token
    assert await worker.verify(alice_token) == alice
    assert await worker.verify(bob_token) == bob
    assert "provider-secret" not in repr(store.records)
    assert alice_token not in repr(store.records)
    assert bob_token not in repr(store.records)
    for context in (
        {**config, "issuer": "https://other.test/oauth"},
        {**config, "resource": "https://mcp.test/other"},
    ):
        assert await OpaqueTokens(store=store, **context).verify(alice_token) is None
    await worker.revoke(alice_token)
    assert await first.verify(alice_token) is None
    assert await first.verify(bob_token) == bob


async def test_opaque_tokens_reject_expired_and_malformed_tokens(monkeypatch):
    now = time.time()
    tokens = OpaqueTokens(issuer="issuer", resource="resource")
    principal = Principal(subject="alice", issuer="issuer", expires_at=now + 30)
    token = await tokens.issue(principal, {})
    for invalid in (None, 123, "", "unknown", token + "x"):
        assert await tokens.verify(invalid) is None
    monkeypatch.setattr("aiohttp_tiny_mcp.oauth.tokens.time.time", lambda: now + 31)
    assert await tokens.verify(token) is None
    for invalid in (
        principal,
        replace(principal, expires_at=None),
        replace(principal, expires_at=float("inf")),
        replace(principal, issuer="other", expires_at=now + 60),
    ):
        with pytest.raises(ValueError):
            await tokens.issue(invalid, {})
