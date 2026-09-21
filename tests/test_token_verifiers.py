"""Static tokens and verified claims preserve identity and credential boundaries."""

from dataclasses import asdict, fields
from hashlib import sha256
from types import MappingProxyType
from unittest.mock import Mock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from test_auth import HEADERS, opened
from test_basic_auth import registry_for
from test_http_sse import OldClient

from aiohttp_tiny_mcp import (
    Authorization,
    Endpoint,
    Principal,
    SseEndpoint,
    StaticVerifier,
    TokenVerifier,
    principal_from_claims,
)
from aiohttp_tiny_mcp import auth as auth_module
from aiohttp_tiny_mcp.protocol.selection import AdapterSet
from aiohttp_tiny_mcp.sessions import SESSION_HEADER
from aiohttp_tiny_mcp.testing import over_http, pick


@pytest.mark.parametrize("token", ["first", "much-longer-token", "last", "missing", ""])
async def test_static_verifier_compares_all_fixed_size_digests(monkeypatch, token):
    mapping = {name: Principal(subject=name) for name in ["first", "much-longer-token", "last"]}
    verifier = StaticVerifier(MappingProxyType(mapping))
    compare = Mock(wraps=auth_module.compare_digest)
    monkeypatch.setattr(auth_module, "compare_digest", compare)
    assert await verifier.verify(token) is mapping.get(token)
    assert compare.call_count == len(mapping)
    for call, expected in zip(compare.call_args_list, mapping, strict=True):
        assert call.args == (sha256(token.encode()).digest(), sha256(expected.encode()).digest())
    assert isinstance(verifier, TokenVerifier)


async def test_static_verifier_snapshots_mapping_and_accepts_unicode():
    principal = Principal(subject="alice")
    mapping = {"секрет": principal}
    verifier = StaticVerifier(mapping)
    mapping.clear()
    assert await verifier.verify("секрет") is principal
    assert await verifier.verify("\ud800") is None
    assert await StaticVerifier({}).verify("anything") is None
    with pytest.raises(ValueError, match="non-empty"):
        StaticVerifier({"": principal})


@pytest.mark.parametrize(
    ("claims", "expected"),
    [
        ({}, Principal()),
        ({"sub": "alice", "iss": "issuer"}, Principal(subject="alice", issuer="issuer")),
        ({"client_id": "cli", "azp": "other"}, Principal(client_id="cli")),
        ({"azp": "cli"}, Principal(client_id="cli")),
        ({"scope": "read  write read"}, Principal(scopes=frozenset({"read", "write"}))),
        ({"scp": ["read", "write"]}, Principal(scopes=frozenset({"read", "write"}))),
        ({"scope": ["read"]}, Principal(scopes=frozenset({"read"}))),
        ({"scope": "", "scp": ["write"]}, Principal()),
        ({"exp": "123.5"}, Principal(expires_at=123.5)),
        ({"namespace": "untrusted"}, Principal()),
    ],
)
def test_default_claims_mapping(claims, expected):
    result = principal_from_claims(MappingProxyType(claims))
    assert result.claims == claims
    assert result.claims is not claims
    assert asdict(result) == {**asdict(expected), "claims": claims}
    assert "sub" not in {field.name for field in fields(result)}
    assert not {"raw", "token", "jwt"} & {field.name for field in fields(result)}


@pytest.mark.parametrize(
    "claims",
    [
        {"sub": 1},
        {"iss": []},
        {"client_id": None},
        {"azp": 1},
        {"scope": {}},
        {"scope": None},
        {"scp": [1]},
        {"exp": None},
        {"exp": "NaN"},
        {"exp": float("inf")},
        {"exp": "invalid"},
        {"exp": True},
        {"exp": 10**400},
    ],
)
def test_malformed_claims_are_rejected(claims):
    with pytest.raises(ValueError):
        principal_from_claims(claims)


@pytest.mark.parametrize("tenant", [None, "team", ""])
@pytest.mark.parametrize("adapter", [*AdapterSet.default().versions, "sse"])
async def test_opaque_token_reaches_only_the_verifier(tenant, adapter):
    token = "opaque.unparseable.credential"
    principal = Principal(subject="alice", namespace=tenant, scopes=frozenset({"read"}))
    registry = registry_for(Authorization(StaticVerifier({token: principal}), "https://test/mcp"))
    headers = {"Authorization": f"Bearer {token}"}
    expected = f"alice:{tenant if tenant is not None else principal.identity}"
    if adapter != "sse":
        async with over_http(registry, adapter=pick(adapter), headers=headers) as client:
            await client.initialize()
            result = await client.call_tool("who", {})
            assert result.content[0].text == expected
    else:
        async with TestServer(SseEndpoint(registry).setup(web.Application())) as server:
            client = OldClient(str(server.make_url("")), headers=headers)
            try:
                await client.open()
                await client.call(
                    "initialize",
                    {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {},
                        "clientInfo": {"name": "test", "version": "1"},
                    },
                )
                result = await client.call("tools/call", {"name": "who", "arguments": {}}, 2)
                assert result["result"]["content"][0]["text"] == expected
            finally:
                await client.close()
    assert token not in repr(asdict(principal))


async def test_static_tokens_sharing_namespace_keep_distinct_session_owners():
    from aiohttp.test_utils import TestClient

    mapping = {name: Principal(subject=name, namespace="team") for name in ["alice", "bob"]}
    registry = registry_for(Authorization(StaticVerifier(mapping), "https://test/mcp"))
    async with TestClient(TestServer(Endpoint(registry).app())) as client:
        session = await opened(client, "alice")
        for name, expected in [("alice", 200), ("bob", 404)]:
            response = await client.post(
                "/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "ping",
                    "params": {},
                },
                headers={
                    **HEADERS,
                    "Authorization": f"Bearer {name}",
                    SESSION_HEADER: session,
                    "MCP-Protocol-Version": "2025-11-25",
                },
            )
            assert response.status == expected
