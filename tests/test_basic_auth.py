"""Public authentication subclasses, Basic credentials, and policy alternatives."""

from __future__ import annotations

from base64 import b64encode
from dataclasses import replace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer, make_mocked_request
from pydantic import BaseModel
from test_http_sse import OldClient

from aiohttp_tiny_mcp import (
    Authentication,
    Authorization,
    BasicAuth,
    Endpoint,
    Principal,
    Registry,
    SseEndpoint,
    StaticBasicAuth,
    Unauthorized,
)
from aiohttp_tiny_mcp.protocol.selection import AdapterSet
from aiohttp_tiny_mcp.storage.hub import NOTIFICATIONS, topic
from aiohttp_tiny_mcp.storage.namespaces import current
from aiohttp_tiny_mcp.storage.sessions import SESSION_HEADER
from aiohttp_tiny_mcp.testing import over_http, pick

pytestmark = pytest.mark.asyncio


def basic(username="alice", password="secret"):
    encoded = b64encode(f"{username}:{password}".encode()).decode()
    return {"Authorization": f"Basic {encoded}"}


class Nothing(BaseModel):
    pass


def registry_for(auth):
    registry = Registry("protected", "1", auth=auth)

    @registry.tool(scopes=["read"])
    async def who(args: Nothing, principal: Principal) -> str:
        return f"{principal.subject}:{current()}"

    return registry


class Users(BasicAuth):
    """A consumer only implements credential verification."""

    async def verify(self, username: str, password: str) -> Principal | None:
        if username in {"alice", "bob"} and password == "secret":
            return Principal(subject=username, scopes=frozenset({"read"}), namespace="team")
        return None


class ApiKey(Authentication):
    """A consumer can inspect the whole request and select its credential source."""

    async def authenticate(self, request: web.Request) -> Principal | None:
        if request.headers.get("X-Key", request.query.get("key")) == "valid":
            return Principal(subject="alice", namespace="team", scopes=frozenset({"read"}))
        return None

    def challenge(self, refusal: Unauthorized) -> str:
        return 'ApiKey realm="example"'


class Tokens:
    async def verify(self, token: str) -> Principal | None:
        if token == "valid":
            return Principal(subject="alice", namespace="team", scopes=frozenset({"read"}))
        return None


def bearer():
    return Authorization(Tokens(), resource="https://example.test/mcp")


@pytest.mark.parametrize("adapter", AdapterSet.default().versions)
@pytest.mark.parametrize("kind", ["static", "subclass", "custom", "mixed"])
async def test_public_authentication_api(adapter, kind):
    policies = {
        "static": StaticBasicAuth(("alice", "secret", ["read"])),
        "subclass": Users(),
        "custom": ApiKey(),
        "mixed": (policy for policy in [Users(), bearer()]),
    }
    headers = basic() if kind in {"static", "subclass"} else {"X-Key": "valid"}
    if kind == "mixed":
        headers = {"Authorization": "Bearer valid"}
    registry = registry_for(policies[kind])
    async with over_http(registry, adapter=pick(adapter), headers=headers) as client:
        await client.initialize()
        result = await client.call_tool("who", {})
        assert not result.is_error
        expected = "|alice" if kind == "static" else "team"
        assert result.content[0].text == f"alice:{expected}"


@pytest.mark.parametrize(
    "header",
    [
        None,
        "Basic !bad",
        "Basic",
        "Bearer secret",
        "Basic YWxpY2U=",
        "Basic /w==",
        "Basic YWxpY2U6c2VjcmV0 extra",
        basic(password="wrong")["Authorization"],
        basic(username="bob")["Authorization"],
        basic(password="sec\nret")["Authorization"],
    ],
)
@pytest.mark.parametrize("route", ["post-mcp", "get-mcp", "get-sse", "post-messages"])
async def test_invalid_basic_credentials_fail_before_dispatch(header, route):
    registry = registry_for(StaticBasicAuth(("alice", "secret", ["read"])))
    app = Endpoint(registry).app()
    SseEndpoint(registry).setup(app)
    method, path = route.split("-", 1)
    headers = {"Accept": "application/json, text/event-stream"}
    if header is not None:
        headers["Authorization"] = header
    async with TestClient(TestServer(app)) as client:
        response = await client.request(
            method,
            "/" + path,
            headers=headers,
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        )
        assert response.status == 401
        assert response.headers["WWW-Authenticate"] == 'Basic realm="mcp", charset="UTF-8"'
        assert not registry.session_store.records
        assert await response.json() == {
            "error": "invalid_credentials",
            "error_description": "valid credentials required",
        }


async def test_basic_utf8_password_colons_and_escaped_realm():
    policy = StaticBasicAuth(("алиса", "секрет:пароль"), realm='a"b\\c')
    request = make_mocked_request("GET", "/", headers=basic("алиса", "секрет:пароль"))
    assert policy.check(await policy.authenticate(request)).subject == "алиса"
    assert policy.challenge(Unauthorized("x", "x")) == 'Basic realm="a\\"b\\\\c", charset="UTF-8"'
    assert "секрет" not in repr(policy)


@pytest.mark.parametrize(
    "options",
    [
        {"username": "a:b"},
        {"password": "\n"},
        {"realm": "x\r\ny"},
        {"realm": "☃"},
        {"username": ""},
        {"password": ""},
    ],
)
async def test_static_basic_rejects_invalid_configuration(options):
    with pytest.raises(ValueError):
        supplied = {"username": "alice", "password": "secret", **options}
        StaticBasicAuth((supplied.pop("username"), supplied.pop("password")), **supplied)


async def test_abstract_classes_require_application_hooks():
    with pytest.raises(TypeError):
        Authentication()
    with pytest.raises(TypeError):
        BasicAuth()


async def test_empty_policy_iterable_is_not_anonymous_access():
    with pytest.raises(ValueError, match="at least one"):
        Registry("protected", "1", auth=iter(()))


async def test_basic_and_custom_policies_do_not_publish_oauth_metadata():
    registry = registry_for([Users(), ApiKey()])
    assert Endpoint(registry).metadata_routes() == []
    assert SseEndpoint(registry).metadata_routes() == []
    async with TestClient(TestServer(Endpoint(registry).app())) as client:
        assert (await client.get("/.well-known/oauth-protected-resource/mcp")).status == 404
        refusal = await client.post("/mcp", json={})
        assert refusal.status == 401
        challenge = refusal.headers["WWW-Authenticate"]
        assert 'Basic realm="mcp"' in challenge
        assert 'ApiKey realm="example"' in challenge
        assert "Bearer" not in challenge


async def test_metadata_with_multiple_policies():
    registry = registry_for([Users(), bearer(), bearer()])
    endpoint = Endpoint(registry)
    assert len(endpoint.metadata_routes()) == 1
    async with TestClient(TestServer(endpoint.app())) as client:
        response = await client.get("/.well-known/oauth-protected-resource/mcp")
        assert response.status == 200
        assert (await response.json())["resource"] == "https://example.test/mcp"
        refusal = await client.post("/mcp", json={})
        assert 'Basic realm="mcp"' in refusal.headers["WWW-Authenticate"]
        assert "Bearer " in refusal.headers["WWW-Authenticate"]
    registry.auth = [bearer(), replace(bearer(), resource_name="different")]
    with pytest.raises(ValueError, match="conflicting"):
        Endpoint(registry).metadata_routes()


async def test_distinct_metadata_paths_serve_their_own_policy():
    registry = registry_for([bearer(), replace(bearer(), resource="https://example.test/other")])
    async with TestClient(TestServer(Endpoint(registry).app())) as http:
        for path in ("mcp", "other"):
            response = await http.get(f"/.well-known/oauth-protected-resource/{path}")
            assert response.status == 200
            assert (await response.json())["resource"] == f"https://example.test/{path}"


async def test_unexpected_authentication_errors_do_not_try_another_policy():
    class Broken(ApiKey):
        async def authenticate(self, request):
            raise RuntimeError("user database unavailable")

    endpoint = Endpoint(registry_for([Broken(), ApiKey()]))
    with pytest.raises(RuntimeError, match="database unavailable"):
        await endpoint.verified(make_mocked_request("GET", "/mcp", headers={"X-Key": "valid"}))


async def test_custom_policy_can_read_query_with_header_precedence():
    app = Endpoint(registry_for(ApiKey())).app()
    async with TestClient(TestServer(app)) as client:
        payload = {"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {}}
        headers = {"MCP-Protocol-Version": "2025-11-25", "Accept": "application/json"}
        good = await client.post("/mcp?key=valid", json=payload, headers=headers)
        assert good.status == 200
        bad = await client.post(
            "/mcp?key=valid", json=payload, headers={**headers, "X-Key": "wrong"}
        )
        assert bad.status == 401


async def test_basic_does_not_implicitly_accept_query_credentials():
    request = make_mocked_request("GET", "/mcp?auth=" + basic()["Authorization"].split()[1])
    assert await Users().authenticate(request) is None


@pytest.mark.parametrize("kind", ["scope", "expired"])
async def test_common_checks_apply_to_custom_policy_and_preserve_scope_failure(kind):
    class Restricted(ApiKey):
        async def authenticate(self, request):
            principal = await super().authenticate(request)
            if principal is not None and kind == "expired":
                return replace(principal, expires_at=1)
            return principal

    policy = Restricted(required_scopes=["write"] if kind == "scope" else [])
    app = Endpoint(registry_for([policy, bearer()])).app()
    async with TestClient(TestServer(app)) as client:
        response = await client.post("/mcp", headers={"X-Key": "valid"}, json={})
        assert response.status == (403 if kind == "scope" else 401)


async def test_tool_scope_check_applies_to_basic_principal():
    registry = registry_for(StaticBasicAuth(("alice", "secret")))
    async with over_http(registry, headers=basic()) as client:
        result = await client.call_tool("who", {})
        assert result.is_error
        assert "read" in result.content[0].text


@pytest.mark.parametrize("headers", [basic(), {"Authorization": "Bearer valid"}])
async def test_basic_and_bearer_over_legacy_sse(headers):
    registry = registry_for([Users(), bearer()])
    async with TestServer(SseEndpoint(registry).setup(web.Application())) as server:
        client = OldClient(str(server.make_url("")), headers=headers)
        try:
            await client.open()
            initialized = await client.call(
                "initialize",
                {
                    "protocolVersion": "2024-11-05",
                    "clientInfo": {"name": "old", "version": "1"},
                    "capabilities": {},
                },
            )
            assert initialized["result"]["protocolVersion"] == "2024-11-05"
            result = await client.call("tools/call", {"name": "who", "arguments": {}}, 2)
            assert result["result"]["content"][0]["text"] == "alice:team"
        finally:
            await client.close()


async def test_shared_namespace_keeps_session_owners_distinct_across_policies():
    registry = registry_for([Users(), bearer()])
    async with TestClient(TestServer(Endpoint(registry).app())) as http:
        opened = await http.post(
            "/mcp",
            headers={**basic(), "Accept": "application/json"},
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-11-25",
                    "clientInfo": {"name": "test", "version": "1"},
                    "capabilities": {},
                },
            },
        )
        session_id = opened.headers[SESSION_HEADER]
        payload = {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}
        headers = {
            "Accept": "application/json",
            SESSION_HEADER: session_id,
            "MCP-Protocol-Version": "2025-11-25",
        }
        stolen = await http.post("/mcp", headers={**headers, **basic("bob")}, json=payload)
        assert stolen.status == 404
        mine = await http.post(
            "/mcp", headers={**headers, "Authorization": "Bearer valid"}, json=payload
        )
        assert mine.status == 200


async def test_hub_namespace_follows_principal_across_policies():
    registry = registry_for([Users(), ApiKey()])

    @registry.tool
    async def emit(args: Nothing) -> str:
        await registry.hub.publish(topic(NOTIFICATIONS), {"method": "changed"})
        return topic(NOTIFICATIONS)

    async with over_http(registry, headers=basic()) as first:
        async with over_http(registry, headers={"X-Key": "valid"}) as second:
            one = await first.call_tool("emit", {})
            two = await second.call_tool("emit", {})
            assert one.content[0].text == two.content[0].text
            events = await registry.hub.poll(one.content[0].text, "", timeout=0)
            assert len(events) == 2


@pytest.mark.parametrize(
    ("username", "password", "expected_scopes"),
    [
        ("alice", "alice-secret", frozenset({"read"})),
        ("bob", "bob-secret", frozenset({"write"})),
        ("guest", "guest-secret", frozenset()),
        ("alice", "bob-secret", None),
        ("bob", "alice-secret", None),
        ("missing", "alice-secret", None),
        ("alice", "invalid", None),
    ],
)
async def test_static_basic_accounts_keep_credentials_and_scopes_separate(
    username, password, expected_scopes, monkeypatch
):
    from unittest.mock import Mock

    from aiohttp_tiny_mcp import auth as auth_module

    policy = StaticBasicAuth(
        ("alice", "alice-secret", {"read"}),
        ("bob", "bob-secret", {"write"}),
        ("guest", "guest-secret"),
    )
    compare = Mock(wraps=auth_module.compare_digest)
    monkeypatch.setattr(auth_module, "compare_digest", compare)
    principal = await policy.verify(username, password)
    if expected_scopes is None:
        assert principal is None
    else:
        assert principal.subject == username
        assert principal.scopes == expected_scopes
        assert principal.identity == f"|{username}"
    assert compare.call_count == 6
    assert all(len(arg) == 32 for call in compare.call_args_list for arg in call.args)
    assert "secret" not in repr(policy)


async def test_static_basic_copies_each_accounts_scope_iterable():
    alice_scopes = ["read"]
    policy = StaticBasicAuth(
        ("alice", "secret", alice_scopes),
        ("bob", "secret", (scope for scope in ["write", "write"])),
    )
    alice_scopes.append("write")
    assert (await policy.verify("alice", "secret")).scopes == frozenset({"read"})
    assert (await policy.verify("bob", "secret")).scopes == frozenset({"write"})
    assert (await policy.verify("bob", "secret")).scopes == frozenset({"write"})


@pytest.mark.parametrize(
    ("accounts", "error"),
    [
        ((), ValueError),
        (("alice", "secret"), TypeError),
        ((("alice",),), TypeError),
        ((("alice", "secret", [], "extra"),), TypeError),
        (((1, "secret"),), TypeError),
        ((("alice", None),), TypeError),
        ((("alice", "secret", "read"),), TypeError),
        ((("alice", "secret", [1]),), TypeError),
        ((("alice", "secret", None),), TypeError),
        ((("alice", "secret"), ("bob", "")), ValueError),
        ((("alice", "secret"), ("alice", "other", ["write"])), ValueError),
    ],
)
async def test_static_basic_rejects_invalid_account_collections(accounts, error):
    with pytest.raises(error):
        StaticBasicAuth(*accounts)


@pytest.mark.parametrize("transport", ["http", "sse"])
@pytest.mark.parametrize("username", ["alice", "bob", "guest"])
async def test_static_basic_account_permissions_over_both_transports(transport, username):
    policy = StaticBasicAuth(
        ("alice", "alice-secret", {"read"}),
        ("bob", "bob-secret", {"write"}),
        ("guest", "guest-secret"),
    )
    registry = registry_for(policy)

    @registry.tool(scopes=["write"])
    async def write(args: Nothing, principal: Principal) -> str:
        return f"{principal.subject}:{current()}"

    @registry.tool
    async def identity(args: Nothing, principal: Principal) -> str:
        return f"{principal.subject}:{current()}"

    expected = f"{username}:|{username}"
    headers = basic(username, f"{username}-secret")
    if transport == "http":
        async with over_http(registry, headers=headers) as client:
            for tool, allowed in [
                ("identity", True),
                ("who", username == "alice"),
                ("write", username == "bob"),
            ]:
                result = await client.call_tool(tool, {})
                assert bool(result.is_error) is not allowed
                if allowed:
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
                        "clientInfo": {"name": "old", "version": "1"},
                    },
                )
                for request_id, (tool, allowed) in enumerate(
                    [
                        ("identity", True),
                        ("who", username == "alice"),
                        ("write", username == "bob"),
                    ],
                    start=2,
                ):
                    response = await client.call(
                        "tools/call", {"name": tool, "arguments": {}}, request_id
                    )
                    result = response["result"]
                    assert bool(result.get("isError")) is not allowed
                    if allowed:
                        assert result["content"][0]["text"] == expected
            finally:
                await client.close()


async def test_required_scopes_apply_to_each_static_account():
    policy = StaticBasicAuth(
        ("alice", "secret", {"read"}),
        ("bob", "secret", {"write"}),
        required_scopes=["read"],
    )
    async with TestClient(TestServer(Endpoint(registry_for(policy)).app())) as client:
        for username, expected in [("alice", 200), ("bob", 403)]:
            response = await client.post(
                "/mcp",
                headers=basic(username),
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "ping",
                    "params": {},
                },
            )
            assert response.status == expected
            if expected == 403:
                assert (await response.json())["error"] == "insufficient_scope"
