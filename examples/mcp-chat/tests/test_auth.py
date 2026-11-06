from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import aiohttp
import jwt
import pytest
from aiohttp.test_utils import TestClient, TestServer
from aiohttp_tiny_mcp import AdapterSet, CallToolResult, Client

from mcp_chat.auth import WEB_AUDIENCE, WEB_COOKIE, issue_token
from mcp_chat.server import create_app
from mcp_chat.store import Store

SECRET = "test-only-signing-key-with-more-than-sixty-four-bytes-for-all-algorithms"
LEGACY = AdapterSet.default().by_version["2025-11-25"]
MODERN = AdapterSet.default().by_version["2026-07-28"]
INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {"protocolVersion": "2025-11-25", "capabilities": {}},
}
ACCEPT = {"Accept": "application/json, text/event-stream"}


@pytest.mark.parametrize("path", ["/auth/login", "/auth/token"])
async def test_old_admin_routes_are_absent(path: str, tmp_path: Path) -> None:
    async with TestClient(
        TestServer(create_app(tmp_path / "chat.sqlite3", jwt_secret=SECRET))
    ) as client:
        response = await client.post(path, json={})
        assert response.status == 404


def claims(**changes: Any) -> dict[str, Any]:
    return {
        "sub": "alice",
        "org": "example.com",
        "iss": "mcp-chat",
        "aud": "mcp-chat",
        "iat": int(time.time()),
        "exp": int(time.time()) + 3600,
        **changes,
    }


@pytest.mark.parametrize("adapter", [LEGACY, MODERN], ids=["legacy", "modern"])
@pytest.mark.parametrize("transport", ["header", "query"])
async def test_valid_token_selects_signed_user(
    tmp_path: Path, adapter: Any, transport: str
) -> None:
    token = issue_token(SECRET, "example.com", "alice")
    async with TestServer(create_app(tmp_path / "chat.sqlite3", jwt_secret=SECRET)) as server:
        url = str(server.make_url("/mcp?org=example.com"))
        headers = {"Authorization": f"bEaReR {token}"} if transport == "header" else {}
        if transport == "query":
            url += f"&token={token}"
        async with aiohttp.ClientSession(headers=headers) as session:
            async with Client(url, adapter, session=session) as client:
                result = await client.call_tool("identity", {})
                assert isinstance(result, CallToolResult)
                assert result.structured_content == {"org": "example.com", "user": "alice"}
                result = await client.call_tool("list_channels", {})
                assert isinstance(result, CallToolResult)
                assert len(result.structured_content["channels"]) == 3


@pytest.mark.parametrize(
    "changes",
    [
        {"exp": 1},
        {"exp": float("inf")},
        {"exp": float("nan")},
        {"exp": True},
        {"iss": "other"},
        {"aud": "other"},
        {"sub": ""},
        {"sub": None},
        {"org": ""},
        {"org": "../other"},
        {"org": None},
    ],
)
async def test_invalid_claims_are_rejected_before_bootstrap(
    tmp_path: Path, changes: dict[str, Any]
) -> None:
    path = tmp_path / "chat.sqlite3"
    token = jwt.encode(claims(**changes), SECRET, algorithm="HS256")
    async with TestClient(TestServer(create_app(path, jwt_secret=SECRET))) as client:
        response = await client.post(
            "/mcp?org=example.com",
            json=INITIALIZE,
            headers={**ACCEPT, "Authorization": f"Bearer {token}"},
        )
        assert response.status == 401
        assert response.headers["WWW-Authenticate"].startswith("Bearer ")
        assert token not in await response.text()
    store = Store(path)
    await store.open()
    try:
        assert await store.list_channels("example.com", "alice") == []
    finally:
        await store.close()


@pytest.mark.parametrize("kind", ["missing", "bad_signature", "none", "wrong_algorithm", "no_exp"])
async def test_invalid_credentials(tmp_path: Path, kind: str) -> None:
    payload = claims()
    if kind == "missing":
        token = None
    elif kind == "none":
        token = jwt.encode(payload, "", algorithm="none")
    elif kind == "bad_signature":
        token = jwt.encode(payload, SECRET + "changed", algorithm="HS256")
    elif kind == "wrong_algorithm":
        token = jwt.encode(payload, SECRET, algorithm="HS384")
    else:
        del payload["exp"]
        token = jwt.encode(payload, SECRET, algorithm="HS256")
    headers = {**ACCEPT, **({"Authorization": f"Bearer {token}"} if token else {})}
    async with TestClient(
        TestServer(create_app(tmp_path / "chat.sqlite3", jwt_secret=SECRET))
    ) as client:
        response = await client.post("/mcp?org=example.com", json=INITIALIZE, headers=headers)
        assert response.status == 401


@pytest.mark.parametrize(
    "query",
    ["", "org=other.com", "org=example.com&user=bob", "org=&user=", "org=../x&org=b&user=a&user=b"],
)
async def test_token_ignores_query_identity(tmp_path: Path, query: str) -> None:
    token = issue_token(SECRET, "example.com", "alice")
    async with TestServer(create_app(tmp_path / "chat.sqlite3", jwt_secret=SECRET)) as server:
        async with aiohttp.ClientSession(headers={"Authorization": f"Bearer {token}"}) as session:
            async with Client(
                str(server.make_url(f"/mcp?{query}")), MODERN, session=session
            ) as client:
                result = await client.call_tool("identity", {})
                assert isinstance(result, CallToolResult)
                assert result.structured_content == {"org": "example.com", "user": "alice"}


@pytest.mark.parametrize("kind", ["both", "query_duplicate", "header_duplicate", "malformed"])
async def test_ambiguous_credentials_are_rejected(tmp_path: Path, kind: str) -> None:
    token = issue_token(SECRET, "example.com", "alice")
    query = "/mcp?org=example.com"
    headers = list(ACCEPT.items())
    if kind == "both":
        query += f"&token={token}"
        headers.append(("Authorization", f"Bearer {token}"))
    elif kind == "query_duplicate":
        query += f"&token={token}&token={token}"
    elif kind == "header_duplicate":
        headers.extend([("Authorization", f"Bearer {token}")] * 2)
    else:
        headers.append(("Authorization", f"Bearer {token} extra"))
    async with TestClient(
        TestServer(create_app(tmp_path / "chat.sqlite3", jwt_secret=SECRET))
    ) as client:
        response = await client.post(query, json=INITIALIZE, headers=headers)
        assert response.status == (401 if kind == "malformed" else 400)
        assert token not in await response.text()


async def test_session_is_bound_to_signed_user_and_org(tmp_path: Path) -> None:
    token = issue_token(SECRET, "example.com", "alice")
    async with TestClient(
        TestServer(create_app(tmp_path / "chat.sqlite3", jwt_secret=SECRET))
    ) as client:
        response = await client.post(
            "/mcp?org=example.com",
            json=INITIALIZE,
            headers={**ACCEPT, "Authorization": f"Bearer {token}"},
        )
        assert response.status == 200
        session_id = response.headers["Mcp-Session-Id"]
        await response.read()
        for org, user in [("example.com", "bob"), ("other.com", "alice")]:
            other_token = issue_token(SECRET, org, user)
            response = await client.post(
                f"/mcp?org={org}",
                json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
                headers={
                    **ACCEPT,
                    "Authorization": f"Bearer {other_token}",
                    "Mcp-Session-Id": session_id,
                    "MCP-Protocol-Version": LEGACY.version,
                },
            )
            assert response.status in {403, 404}


async def test_ui_is_public_but_demo_rejects_credentials(tmp_path: Path) -> None:
    token = issue_token(SECRET, "example.com", "alice")
    async with TestClient(
        TestServer(create_app(tmp_path / "chat.sqlite3", jwt_secret=SECRET))
    ) as client:
        assert (await client.get("/ui/")).status == 200
        assert (await client.get("/ui/static/app.js")).status == 200
    async with TestClient(TestServer(create_app(tmp_path / "demo.sqlite3"))) as client:
        response = await client.post(
            f"/mcp?org=example.com&token={token}", json=INITIALIZE, headers=ACCEPT
        )
        assert response.status == 401


@pytest.mark.parametrize("ttl", [0, -1, 86401, True])
def test_token_issue_requires_valid_lifetime(ttl: int) -> None:
    with pytest.raises(ValueError):
        issue_token(SECRET, "example.com", "alice", ttl)


def test_token_issue_and_server_require_strong_key(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="32 bytes"):
        issue_token("short", "example.com", "alice")
    with pytest.raises(ValueError, match="32 bytes"):
        create_app(tmp_path / "chat.sqlite3", jwt_secret="short")


def test_cli_issues_token_and_redacts_help() -> None:
    env = {**os.environ, "MCP_CHAT_JWT_SECRET": SECRET}
    issued = subprocess.run(
        [
            sys.executable,
            "-m",
            "mcp_chat",
            "--issue-token",
            "--org",
            "example.com",
            "--user",
            "alice",
        ],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    decoded = jwt.decode(issued.stdout.strip(), SECRET, algorithms=["HS256"], audience="mcp-chat")
    assert decoded["sub"] == "alice"
    assert decoded["org"] == "example.com"
    assert len(issued.stdout.strip().splitlines()) == 1
    help_result = subprocess.run(
        [sys.executable, "-m", "mcp_chat", "--help"],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert SECRET not in help_result.stdout + help_result.stderr
    assert SECRET not in issued.stderr


async def test_web_login_and_token_issuance(tmp_path: Path) -> None:
    async with TestClient(
        TestServer(create_app(tmp_path / "chat.sqlite3", jwt_secret=SECRET))
    ) as client:
        origin = str(client.make_url("/")).rstrip("/")
        response = await client.post(
            "/admin/auth",
            json={"secret": SECRET, "org": "example.com", "user": "alice"},
            headers={"Origin": origin},
        )
        assert response.status == 200
        body = await response.json()
        assert SECRET not in str(body)
        assert body["expires_in"] == 3600
        assert response.headers["Cache-Control"] == "no-store"
        cookie = response.cookies[WEB_COOKIE]
        assert cookie["httponly"]
        assert cookie["samesite"] == "Strict"
        assert cookie["path"] == "/admin"
        assert cookie["max-age"] == "3600"
        web_claims = jwt.decode(cookie.value, SECRET, algorithms=["HS256"], audience=WEB_AUDIENCE)
        assert web_claims["sub"] == "web-operator"
        scoped = jwt.decode(body["token"], SECRET, algorithms=["HS256"], audience="mcp-chat")
        assert scoped["sub"] == "alice"
        assert scoped["org"] == "example.com"
        response = await client.post(
            "/admin/token",
            json={"org": "other.example", "user": "bob", "ttl_seconds": 60},
            headers={"Origin": origin},
        )
        assert response.status == 200
        body = await response.json()
        scoped = jwt.decode(body["token"], SECRET, algorithms=["HS256"], audience="mcp-chat")
        assert scoped["org"] == "other.example"
        assert scoped["sub"] == "bob"
        assert scoped["exp"] - scoped["iat"] == body["expires_in"] == 60
        response = await client.post("/mcp?org=example.com", json=INITIALIZE, headers=ACCEPT)
        assert response.status == 401
        response = await client.post(
            "/mcp?org=example.com",
            json=INITIALIZE,
            headers={**ACCEPT, "Authorization": f"Bearer {cookie.value}"},
        )
        assert response.status == 401


@pytest.mark.parametrize("path", ["/admin/auth", "/admin/token"])
async def test_auth_endpoints_are_absent_in_demo(tmp_path: Path, path: str) -> None:
    async with TestClient(TestServer(create_app(tmp_path / "chat.sqlite3"))) as client:
        response = await client.post(path, json={})
        assert response.status == 404


async def test_web_login_rejects_wrong_secret_without_cookie(tmp_path: Path) -> None:
    async with TestClient(
        TestServer(create_app(tmp_path / "chat.sqlite3", jwt_secret=SECRET))
    ) as client:
        response = await client.post(
            "/admin/auth", json={"secret": "wrong-secret", "org": "example.com", "user": "alice"}
        )
        assert response.status == 401
        assert "wrong-secret" not in await response.text()
        assert not response.cookies
        assert response.headers["Cache-Control"] == "no-store"


@pytest.mark.parametrize("path", ["/admin/auth", "/admin/token"])
async def test_auth_rejects_foreign_origins_and_form_posts(tmp_path: Path, path: str) -> None:
    async with TestClient(
        TestServer(create_app(tmp_path / "chat.sqlite3", jwt_secret=SECRET))
    ) as client:
        body = {"org": "example.com", "user": "alice"}
        if path == "/admin/auth":
            body["secret"] = SECRET
        response = await client.post(path, json=body, headers={"Origin": "https://foreign.example"})
        assert response.status == 403
        response = await client.post(path, json=body, headers={"Sec-Fetch-Site": "cross-site"})
        assert response.status == 403
        response = await client.post(path, data=body)
        assert response.status == 415


@pytest.mark.parametrize("kind", ["missing", "invalid", "expired", "mcp", "nonfinite"])
async def test_web_token_requires_valid_web_cookie(tmp_path: Path, kind: str) -> None:
    if kind == "missing":
        cookie = None
    elif kind == "invalid":
        cookie = "not-a-jwt"
    elif kind == "mcp":
        cookie = issue_token(SECRET, "example.com", "alice")
    else:
        cookie = jwt.encode(
            claims(
                sub="web-operator", aud=WEB_AUDIENCE, exp=1 if kind == "expired" else float("inf")
            ),
            SECRET,
            algorithm="HS256",
        )
    async with TestClient(
        TestServer(create_app(tmp_path / "chat.sqlite3", jwt_secret=SECRET))
    ) as client:
        response = await client.post(
            "/admin/token",
            json={"org": "example.com", "user": "alice"},
            cookies={WEB_COOKIE: cookie} if cookie else {},
        )
        assert response.status == 401


@pytest.mark.parametrize(
    "body", [{"org": "example.com"}, {"org": "example.com", "user": "alice", "ttl_seconds": 0}]
)
async def test_web_token_validates_context_and_ttl(tmp_path: Path, body: dict[str, Any]) -> None:
    async with TestClient(
        TestServer(create_app(tmp_path / "chat.sqlite3", jwt_secret=SECRET))
    ) as client:
        response = await client.post(
            "/admin/auth", json={"secret": SECRET, "org": "example.com", "user": "alice"}
        )
        assert response.status == 200
        await response.read()
        response = await client.post("/admin/token", json=body)
        assert response.status == 400
