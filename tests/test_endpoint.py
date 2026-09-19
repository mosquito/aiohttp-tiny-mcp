from __future__ import annotations

import asyncio
import json

import pytest
from aiohttp.test_utils import TestClient, TestServer
from pydantic import BaseModel

from aiohttp_tiny_mcp import Endpoint, Registry

pytestmark = pytest.mark.asyncio


class Slow(BaseModel):
    pass


async def post26(client, method, params, *, id=1, name=None, extra_headers=None):
    headers = {"MCP-Protocol-Version": "2026-07-28", "Mcp-Method": method}
    if name is not None:
        headers["Mcp-Name"] = name
    headers.update(extra_headers or {})
    params = dict(params)
    meta = dict(params.get("_meta", {}))
    meta.setdefault("io.modelcontextprotocol/protocolVersion", "2026-07-28")
    meta.setdefault("io.modelcontextprotocol/clientCapabilities", {})
    params["_meta"] = meta
    return await client.post(
        "/mcp",
        headers=headers,
        json={"jsonrpc": "2.0", "id": id, "method": method, "params": params},
    )


async def post_legacy(client, method, params, *, id=1, version="2025-11-25"):
    headers = {"MCP-Protocol-Version": version} if version else {}
    return await client.post(
        "/mcp",
        headers=headers,
        json={"jsonrpc": "2.0", "id": id, "method": method, "params": params},
    )


async def test_discover_2026(client):
    resp = await post26(client, "server/discover", {})
    assert resp.status == 200
    body = await resp.json()
    assert body["result"]["resultType"] == "complete"
    assert "2026-07-28" in body["result"]["supportedVersions"]
    assert body["result"]["ttlMs"] == 0
    assert body["result"]["cacheScope"] == "private"
    assert body["result"]["capabilities"]["tools"]["listChanged"] is True
    assert body["result"]["capabilities"]["resources"]["subscribe"] is True


async def test_initialize_legacy(client):
    resp = await post_legacy(client, "initialize", {"protocolVersion": "2025-11-25"})
    assert resp.status == 200
    body = await resp.json()
    assert body["result"]["protocolVersion"] == "2025-11-25"
    assert body["result"]["serverInfo"]["name"] == "demo"


async def test_call_tool_success_2026(client):
    resp = await post26(
        client, "tools/call", {"name": "add", "arguments": {"a": 2, "b": 3}}, name="add"
    )
    body = await resp.json()
    assert body["result"]["structuredContent"] == {"result": 5}
    assert body["result"]["isError"] is False


async def test_call_tool_success_legacy(client):
    resp = await post_legacy(client, "tools/call", {"name": "add", "arguments": {"a": 10, "b": 20}})
    body = await resp.json()
    assert body["result"]["structuredContent"] == {"result": 30}


async def test_non_object_structured_content_unrestricted_on_2026(client):
    resp = await post26(client, "tools/call", {"name": "listy", "arguments": {}}, name="listy")
    body = await resp.json()
    assert body["result"]["structuredContent"] == [1, 2, 3]


async def test_non_object_structured_content_dropped_on_legacy(client):
    resp = await post_legacy(client, "tools/call", {"name": "listy", "arguments": {}})
    body = await resp.json()
    assert "structuredContent" not in body["result"]
    assert "[1, 2, 3]" in body["result"]["content"][0]["text"]


async def test_tool_argument_validation_is_a_result_not_an_error(client):
    """docs/reference/adapters.md: bad tool arguments are isError=true, not an RPC error."""
    resp = await post26(
        client, "tools/call", {"name": "add", "arguments": {"a": "not-a-number"}}, name="add"
    )
    assert resp.status == 200
    body = await resp.json()
    assert "error" not in body
    assert body["result"]["isError"] is True


async def test_tool_exception_is_a_result_not_an_error(client):
    resp = await post26(client, "tools/call", {"name": "boom", "arguments": {}}, name="boom")
    assert resp.status == 200
    body = await resp.json()
    assert body["result"]["isError"] is True
    assert "kaboom" in body["result"]["content"][0]["text"]


async def test_call_unknown_tool(client):
    resp = await post26(client, "tools/call", {"name": "nope", "arguments": {}}, name="nope")
    assert resp.status == 404
    body = await resp.json()
    assert body["error"]["code"] == -32601


async def test_unknown_method_404_on_2026(client):
    resp = await post26(client, "bogus/method", {})
    assert resp.status == 404
    body = await resp.json()
    assert body["error"]["code"] == -32601


async def test_unknown_method_200_on_legacy(client):
    resp = await post_legacy(client, "bogus/method", {})
    assert resp.status == 200
    body = await resp.json()
    assert body["error"]["code"] == -32601


async def test_invalid_json_is_parse_error_before_modern_header_checks(client):
    resp = await client.post(
        "/mcp",
        headers={
            "MCP-Protocol-Version": "2026-07-28",
            "Mcp-Method": "tools/list",
            "Content-Type": "application/json",
        },
        data=b"{",
    )
    assert resp.status == 400
    body = await resp.json()
    assert body["id"] is None
    assert body["error"]["code"] == -32700


async def test_valid_json_null_is_invalid_request_not_parse_error(client):
    resp = await client.post(
        "/mcp",
        headers={"MCP-Protocol-Version": "2025-03-26", "Content-Type": "application/json"},
        data=b"null",
    )
    body = await resp.json()
    assert body["id"] is None
    assert body["error"]["code"] == -32600


@pytest.mark.parametrize("bad_id", [None, True, {"bad": "id"}, [1]])
async def test_invalid_request_ids_reply_with_null_id(client, bad_id):
    resp = await post_legacy(client, "ping", {}, id=bad_id)
    assert resp.status == 400
    body = await resp.json()
    assert body["id"] is None
    assert body["error"]["code"] == -32600


async def test_valid_notification_still_has_no_response(client):
    resp = await client.post(
        "/mcp",
        headers={"MCP-Protocol-Version": "2025-11-25"},
        json={"jsonrpc": "2.0", "method": "bogus", "params": {}},
    )
    assert resp.status == 202
    assert await resp.text() == ""


async def test_modern_request_requires_body_metadata(client):
    resp = await client.post(
        "/mcp",
        headers={"MCP-Protocol-Version": "2026-07-28", "Mcp-Method": "tools/list"},
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
    )
    body = await resp.json()
    assert resp.status == 400
    assert body["error"]["code"] == -32600


async def test_modern_request_requires_client_capabilities(client):
    resp = await client.post(
        "/mcp",
        headers={"MCP-Protocol-Version": "2026-07-28", "Mcp-Method": "tools/list"},
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/list",
            "params": {"_meta": {"io.modelcontextprotocol/protocolVersion": "2026-07-28"}},
        },
    )
    body = await resp.json()
    assert resp.status == 400
    assert body["error"]["code"] == -32600


async def test_conflicting_header_and_body_versions_are_rejected_at_endpoint(client):
    resp = await client.post(
        "/mcp",
        headers={"MCP-Protocol-Version": "2025-11-25"},
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/list",
            "params": {
                "_meta": {
                    "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                    "io.modelcontextprotocol/clientCapabilities": {},
                }
            },
        },
    )
    body = await resp.json()
    assert resp.status == 400
    assert body["error"]["code"] == -32020


async def test_legacy_initialize_result_is_not_shared_across_revisions(client):
    old = await post_legacy(
        client,
        "initialize",
        {"protocolVersion": "2025-06-18"},
        version="2025-06-18",
    )
    new = await post_legacy(
        client,
        "initialize",
        {"protocolVersion": "2025-11-25"},
        version="2025-11-25",
    )
    assert (await old.json())["result"]["protocolVersion"] == "2025-06-18"
    assert (await new.json())["result"]["protocolVersion"] == "2025-11-25"


async def test_unsupported_version(client):
    resp = await post_legacy(client, "ping", {}, version="1999-01-01")
    assert resp.status == 400
    body = await resp.json()
    assert body["error"]["code"] == -32022
    assert body["error"]["data"]["supported"]
    assert body["error"]["data"]["requested"] == "1999-01-01"


async def test_query_pin_overrides_header_and_body(client):
    """The deployment URL pin outranks client version claims."""
    resp = await client.post(
        "/mcp?mcp=2025-11-25",
        headers={"MCP-Protocol-Version": "2026-07-28", "Mcp-Method": "tools/list"},
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/list",
            "params": {"_meta": {"io.modelcontextprotocol/protocolVersion": "2026-07-28"}},
        },
    )
    assert resp.status == 200
    body = await resp.json()
    assert "resultType" not in body["result"]
    assert body["result"]["tools"]


async def test_query_pin_selects_the_modern_revision(client):
    """A pin selects the codec but does not waive its _meta requirements."""
    resp = await client.post(
        "/mcp?mcp=2026-07-28",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
    )
    body = await resp.json()
    assert "protocol version" in body["error"]["message"]


async def test_unsupported_query_pin_is_rejected(client):
    resp = await client.post(
        "/mcp?mcp=1999-01-01",
        json={"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {}},
    )
    assert resp.status == 400
    body = await resp.json()
    assert body["error"]["code"] == -32022
    assert body["error"]["data"]["requested"] == "1999-01-01"


async def test_missing_required_capability_on_2026(client):
    """Missing elicitation must produce MissingRequiredClientCapabilityError."""
    resp = await post26(
        client, "tools/call", {"name": "blind_ask", "arguments": {}}, name="blind_ask"
    )
    assert resp.status == 400
    body = await resp.json()
    assert body["error"]["code"] == -32021
    assert body["error"]["data"]["requiredCapabilities"] == {"elicitation": {}}


async def test_get_is_405(client):
    resp = await client.get("/mcp")
    assert resp.status == 405


async def test_delete_is_405(client):
    resp = await client.delete("/mcp")
    assert resp.status == 405


async def test_a_session_id_is_minted_by_the_handshake_and_by_nothing_else(client):
    """Never echo an invented session id as if initialization had succeeded."""
    resp = await post_legacy(client, "ping", {})
    assert "Mcp-Session-Id" not in resp.headers

    resp2 = await client.post(
        "/mcp",
        headers={"MCP-Protocol-Version": "2025-11-25", "Mcp-Session-Id": "client-supplied-id"},
        json={"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {}},
    )
    assert resp2.status == 200
    assert "Mcp-Session-Id" not in resp2.headers


async def test_origin_header_rejected_by_default(client):
    """Reject unapproved cross-origin browser requests."""
    resp = await client.post(
        "/mcp",
        headers={"Origin": "https://evil.example"},
        json={"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {}},
    )
    assert resp.status == 403


async def test_no_origin_header_is_unaffected(client):
    resp = await post_legacy(client, "ping", {})
    assert resp.status == 200


async def test_http_binding_rejects_wrong_content_type(client):
    resp = await client.post(
        "/mcp",
        headers={"Content-Type": "text/plain"},
        data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {}}),
    )
    assert resp.status == 415


async def test_http_binding_rejects_unacceptable_response_type(client):
    resp = await post_legacy(
        client,
        "ping",
        {},
        version="2025-11-25",
    )
    assert resp.status == 200

    resp = await client.post(
        "/mcp",
        headers={"MCP-Protocol-Version": "2025-11-25", "Accept": "text/plain"},
        json={"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {}},
    )
    assert resp.status == 406


async def test_http_binding_respects_accept_quality_and_wildcards(client):
    rejected = await client.post(
        "/mcp",
        headers={
            "MCP-Protocol-Version": "2025-11-25",
            "Accept": "application/json;q=0, text/plain",
        },
        json={"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {}},
    )
    assert rejected.status == 406

    accepted = await client.post(
        "/mcp",
        headers={"MCP-Protocol-Version": "2025-11-25", "Accept": "application/*"},
        json={"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {}},
    )
    assert accepted.status == 200


async def test_origin_header_allowed_when_allowlisted(registry: Registry):
    endpoint = Endpoint(registry, allowed_origins={"https://trusted.example"})
    async with TestClient(TestServer(endpoint.app("/mcp"))) as client:
        resp = await client.post(
            "/mcp",
            headers={"Origin": "https://trusted.example"},
            json={"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {}},
        )
        assert resp.status == 200


@pytest.mark.parametrize(
    ("origin", "status"),
    [
        ("https://app.example.com", 200),
        ("https://a.b.example.com", 200),
        ("https://example.com", 403),
        ("https://app.example.com.evil.example", 403),
        ("http://app.example.com", 403),
        ("https://app.example.com:8443", 403),
    ],
)
async def test_wildcard_origins(registry: Registry, origin: str, status: int):
    """`*` is one label, `**` one or more; the scheme and any port stay literal."""
    endpoint = Endpoint(
        registry, allowed_origins={"https://*.example.com", "https://**.example.com"}
    )
    async with TestClient(TestServer(endpoint.app("/mcp"))) as client:
        resp = await client.post(
            "/mcp",
            headers={"Origin": origin},
            json={"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {}},
        )
        assert resp.status == status


@pytest.mark.parametrize(
    ("origin", "status"),
    [
        ("https://app.eu.example.com", 200),
        ("https://app.example.com", 403),
        ("https://app.eu.west.example.com", 403),
        ("https://api.eu.example.com", 403),
    ],
)
async def test_wildcard_in_the_middle_of_the_host(registry: Registry, origin: str, status: int):
    endpoint = Endpoint(registry, allowed_origins={"https://app.*.example.com"})
    async with TestClient(TestServer(endpoint.app("/mcp"))) as client:
        resp = await client.post(
            "/mcp",
            headers={"Origin": origin},
            json={"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {}},
        )
        assert resp.status == status


async def test_origin_header_accepted_when_trust_proxy_validation(registry: Registry):
    endpoint = Endpoint(registry, trust_proxy_origin_validation=True)
    async with TestClient(TestServer(endpoint.app("/mcp"))) as client:
        resp = await client.post(
            "/mcp",
            headers={"Origin": "https://anything.example"},
            json={"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {}},
        )
        assert resp.status == 200


async def test_ping_and_resource_subscribe_removed_on_2026(client):
    """Modern clients use subscriptions/listen; removed methods return UNKNOWN_METHOD."""
    for method in ("ping", "resources/subscribe", "resources/unsubscribe"):
        resp = await post26(client, method, {}, name=None)
        assert resp.status == 404
        body = await resp.json()
        assert body["error"]["code"] == -32601


async def test_unknown_method_preserves_request_id_2026(client):
    """Decode failures retain a successfully parsed request id."""
    resp = await client.post(
        "/mcp",
        headers={"MCP-Protocol-Version": "2026-07-28", "Mcp-Method": "bogus"},
        json={
            "jsonrpc": "2.0",
            "id": 42,
            "method": "bogus",
            "params": {
                "_meta": {
                    "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                    "io.modelcontextprotocol/clientCapabilities": {},
                }
            },
        },
    )
    body = await resp.json()
    assert body["id"] == 42
    assert body["error"]["code"] == -32601


async def test_unknown_method_preserves_request_id_legacy(client):
    resp = await post_legacy(client, "bogus", {}, id=7)
    body = await resp.json()
    assert body["id"] == 7


async def test_malformed_base64_mcp_name_header_is_rejected_cleanly(client):
    """Malformed header encoding must return HeaderMismatch, not an HTTP 500."""
    resp = await client.post(
        "/mcp",
        headers={
            "MCP-Protocol-Version": "2026-07-28",
            "Mcp-Method": "resources/read",
            "Mcp-Name": "=?base64?not-valid-base64???",
        },
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "resources/read",
            "params": {
                "uri": "config://app",
                "_meta": {
                    "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                    "io.modelcontextprotocol/clientCapabilities": {},
                },
            },
        },
    )
    assert resp.status == 400
    body = await resp.json()
    assert body["error"]["code"] == -32020


async def test_mcp_parameter_header_is_required_and_must_match(client):
    missing = await post26(
        client, "tools/call", {"name": "routed", "arguments": {"region": "eu"}}, name="routed"
    )
    assert missing.status == 400
    assert (await missing.json())["error"]["code"] == -32020

    mismatch = await post26(
        client,
        "tools/call",
        {"name": "routed", "arguments": {"region": "eu"}},
        name="routed",
        extra_headers={"Mcp-Param-Region": "us"},
    )
    assert mismatch.status == 400
    assert (await mismatch.json())["error"]["code"] == -32020

    matching = await post26(
        client,
        "tools/call",
        {"name": "routed", "arguments": {"region": "eu"}},
        name="routed",
        extra_headers={"Mcp-Param-Region": "eu"},
    )
    assert matching.status == 200


async def test_mcp_parameter_header_strict_base64_decoding(client):
    malformed = await post26(
        client,
        "tools/call",
        {"name": "routed", "arguments": {"region": "日本語"}},
        name="routed",
        extra_headers={"Mcp-Param-Region": "=?base64?not!!!base64?="},
    )
    assert malformed.status == 400
    assert (await malformed.json())["error"]["code"] == -32020

    matching = await post26(
        client,
        "tools/call",
        {"name": "routed", "arguments": {"region": "日本語"}},
        name="routed",
        extra_headers={"Mcp-Param-Region": "=?base64?5pel5pys6Kqe?="},
    )
    assert matching.status == 200


async def test_missing_mcp_method_header_is_rejected(client):
    resp = await client.post(
        "/mcp",
        headers={"MCP-Protocol-Version": "2026-07-28"},
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "ping",
            "params": {
                "_meta": {
                    "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                    "io.modelcontextprotocol/clientCapabilities": {},
                }
            },
        },
    )
    assert resp.status == 400
    body = await resp.json()
    assert body["error"]["code"] == -32020


async def test_mrtr_round_trip(client):
    resp = await post26(
        client,
        "tools/call",
        {
            "name": "confirm",
            "arguments": {"service": "web"},
            "_meta": {"io.modelcontextprotocol/clientCapabilities": {"elicitation": {}}},
        },
        name="confirm",
    )
    body = await resp.json()
    assert body["result"]["resultType"] == "input_required"
    state = body["result"]["requestState"]

    resp2 = await post26(
        client,
        "tools/call",
        {
            "name": "confirm",
            "arguments": {"service": "web"},
            "_meta": {},
            "inputResponses": {"confirm": {"action": "accept", "content": {"ok": True}}},
            "requestState": state,
        },
        name="confirm",
    )
    body2 = await resp2.json()
    assert "structuredContent" not in body2["result"]
    assert "deployed web" in body2["result"]["content"][0]["text"]


@pytest.mark.parametrize("returned_state", ["not-a-token", "v1.bad.bad"])
async def test_mrtr_rejects_malformed_or_tampered_request_state(client, returned_state):
    resp = await post26(
        client,
        "tools/call",
        {
            "name": "confirm",
            "arguments": {"service": "web"},
            "_meta": {"io.modelcontextprotocol/clientCapabilities": {"elicitation": {}}},
            "inputResponses": {"confirm": {"action": "accept", "content": {"ok": True}}},
            "requestState": returned_state,
        },
        name="confirm",
    )
    body = await resp.json()
    assert body["error"]["code"] == -32602


async def test_mrtr_request_state_is_bound_to_original_arguments(client):
    first = await post26(
        client,
        "tools/call",
        {
            "name": "confirm",
            "arguments": {"service": "web"},
            "_meta": {"io.modelcontextprotocol/clientCapabilities": {"elicitation": {}}},
        },
        name="confirm",
    )
    state = (await first.json())["result"]["requestState"]
    retry = await post26(
        client,
        "tools/call",
        {
            "name": "confirm",
            "arguments": {"service": "db"},
            "inputResponses": {"confirm": {"action": "accept", "content": {"ok": True}}},
            "requestState": state,
        },
        name="confirm",
    )
    assert (await retry.json())["error"]["code"] == -32602


async def test_mrtr_decline(client):
    """Declined/cancelled answers remain present with empty content to prevent repeated asking."""
    resp = await post26(
        client,
        "tools/call",
        {
            "name": "confirm",
            "arguments": {"service": "web"},
            "_meta": {"io.modelcontextprotocol/clientCapabilities": {"elicitation": {}}},
        },
        name="confirm",
    )
    state = (await resp.json())["result"]["requestState"]

    resp2 = await post26(
        client,
        "tools/call",
        {
            "name": "confirm",
            "arguments": {"service": "web"},
            "_meta": {},
            "inputResponses": {"confirm": {"action": "decline"}},
            "requestState": state,
        },
        name="confirm",
    )
    body2 = await resp2.json()
    assert "cancelled" in body2["result"]["content"][0]["text"]


async def test_mrtr_unsupported_on_legacy(client):
    resp = await post_legacy(
        client, "tools/call", {"name": "confirm", "arguments": {"service": "x"}}
    )
    body = await resp.json()
    assert body["result"]["isError"] is False
    assert "cannot ask" in body["result"]["content"][0]["text"]


async def test_mrtr_unsupported_without_elicitation_capability_on_2026(client):
    """Do not send input_required to a client that has not declared elicitation."""
    resp = await post26(
        client, "tools/call", {"name": "confirm", "arguments": {"service": "x"}}, name="confirm"
    )
    body = await resp.json()
    assert body["result"]["resultType"] == "complete"
    assert body["result"]["isError"] is False
    assert "cannot ask" in body["result"]["content"][0]["text"]


async def test_read_fixed_resource(client):
    resp = await post26(client, "resources/read", {"uri": "config://app"}, name="config://app")
    body = await resp.json()
    assert body["result"]["contents"][0]["uri"] == "config://app"


async def test_read_templated_resource(client):
    resp = await post26(client, "resources/read", {"uri": "res://items/42"}, name="res://items/42")
    body = await resp.json()
    assert "item-42" in body["result"]["contents"][0]["text"]


async def test_read_missing_resource(client):
    resp = await post26(client, "resources/read", {"uri": "res://nope"}, name="res://nope")
    assert resp.status == 404


async def test_read_resource_defaults_to_private_uncached(client):
    """Request-dependent resource reads require explicit opt-in to public caching."""
    resp = await post26(client, "resources/read", {"uri": "config://app"}, name="config://app")
    body = await resp.json()
    assert body["result"]["ttlMs"] == 0
    assert body["result"]["cacheScope"] == "private"


async def test_read_resource_honors_cache_opt_in(client):
    resp = await post26(client, "resources/read", {"uri": "res://public"}, name="res://public")
    body = await resp.json()
    assert body["result"]["ttlMs"] == 60_000
    assert body["result"]["cacheScope"] == "public"


async def test_list_prompts_and_get_prompt(client):
    resp = await post26(client, "prompts/list", {})
    body = await resp.json()
    assert body["result"]["prompts"][0]["name"] == "greet"

    resp2 = await post26(
        client, "prompts/get", {"name": "greet", "arguments": {"language": "en"}}, name="greet"
    )
    body2 = await resp2.json()
    assert "en speaker" in body2["result"]["messages"][0]["content"]["text"]


async def test_completion(client):
    resp = await post26(
        client,
        "completion/complete",
        {
            "ref": {"type": "ref/prompt", "name": "greet"},
            "argument": {"name": "language", "value": "py"},
        },
    )
    body = await resp.json()
    assert body["result"]["completion"]["values"] == ["python"]


async def test_batch_only_on_2025_03_26(client):
    resp = await client.post(
        "/mcp",
        headers={"MCP-Protocol-Version": "2025-03-26"},
        json=[
            {"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {}},
            {"jsonrpc": "2.0", "id": 2, "method": "ping", "params": {}},
        ],
    )
    assert resp.status == 200
    body = await resp.json()
    assert isinstance(body, list)
    assert len(body) == 2


async def test_batch_rejected_on_2025_11_25(client):
    resp = await client.post(
        "/mcp",
        headers={"MCP-Protocol-Version": "2025-11-25"},
        json=[{"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {}}],
    )
    assert resp.status == 400


async def test_batch_one_bad_item_does_not_take_down_its_siblings(client):
    resp = await client.post(
        "/mcp",
        headers={"MCP-Protocol-Version": "2025-03-26"},
        json=[
            {"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {}},
            {"jsonrpc": "2.0", "id": 2, "method": "bogus/method", "params": {}},
            "not even an object",
            {"jsonrpc": "2.0", "id": 3, "method": "ping", "params": {}},
        ],
    )
    assert resp.status == 200
    body = await resp.json()
    assert isinstance(body, list)
    assert len(body) == 4
    assert any(entry.get("id") is None and entry["error"]["code"] == -32600 for entry in body)
    by_id = {entry["id"]: entry for entry in body if entry.get("id") is not None}
    assert "result" in by_id[1]
    assert by_id[2]["error"]["code"] == -32601
    assert "result" in by_id[3]


async def test_list_tools_caching_is_deterministic(client):
    resp1 = await post26(client, "tools/list", {})
    resp2 = await post26(client, "tools/list", {})
    assert await resp1.text() == await resp2.text()


async def test_streaming_tool(client):
    resp = await post26(
        client, "tools/call", {"name": "counter", "arguments": {"a": 0, "b": 2}}, name="counter"
    )
    assert resp.status == 200
    text = await resp.text()
    assert "notifications/progress" in text
    assert "counted 2" in text


async def test_streaming_tool_requires_sse_accept(client):
    resp = await post26(
        client,
        "tools/call",
        {"name": "counter", "arguments": {"a": 0, "b": 2}},
        name="counter",
        extra_headers={"Accept": "application/json"},
    )
    assert resp.status == 406


@pytest.mark.parametrize("handler_cancellation", [False, True])
async def test_stream_close_cancels_modern_streaming_handler(registry, handler_cancellation):
    started = asyncio.Event()
    stopped = asyncio.Event()

    @registry.tool(streaming=True)
    async def slow(args: Slow) -> str:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()
        return "unreachable"

    class CancellationServer(TestServer):
        async def _make_runner(self, **kwargs):
            kwargs["handler_cancellation"] = handler_cancellation
            return await super()._make_runner(**kwargs)

    async with TestClient(CancellationServer(Endpoint(registry).app())) as client:
        resp = await post26(client, "tools/call", {"name": "slow", "arguments": {}}, name="slow")
        await asyncio.wait_for(started.wait(), timeout=1)
        resp.close()
        await asyncio.wait_for(stopped.wait(), timeout=2)


async def test_the_modern_revision_does_not_set_a_log_level_for_a_session(client):
    """Modern log level is per request."""
    response = await post26(client, "logging/setLevel", {"level": "debug"})
    assert (await response.json())["error"]["code"] == -32601


@pytest.mark.parametrize("method", ["resources/subscribe", "resources/unsubscribe"])
async def test_the_modern_revision_does_not_subscribe_one_at_a_time(client, method):
    """Modern subscriptions/listen names all desired subscriptions in one request."""
    response = await post26(client, method, {"uri": "config://app"})
    assert (await response.json())["error"]["code"] == -32601


@pytest.mark.parametrize("version", ["2025-03-26", "2025-06-18", "2025-11-25"])
async def test_subscribing_without_a_session_is_refused(client, version):
    """Legacy subscriptions need shared session storage to reach the streaming node."""
    response = await post_legacy(
        client, "resources/subscribe", {"uri": "config://app"}, version=version
    )
    assert "error" in await response.json()


async def test_accepted_confirmation_with_empty_content(client):
    """Empty content may mean acceptance or refusal; the action decides."""
    resp = await post26(
        client,
        "tools/call",
        {
            "name": "gate",
            "arguments": {},
            "inputResponses": {"confirm": {"action": "accept", "content": {}}},
        },
        name="gate",
    )
    body = await resp.json()
    assert body["result"]["content"][0]["text"] == "accepted"


@pytest.mark.parametrize(
    "answer",
    [
        {"action": "accept"},
        {"action": "accept", "content": {}},
        {"action": "accept", "content": {"unrelated": 1}},
    ],
)
async def test_every_accept_shape_counts_as_agreement(client, answer):
    resp = await post26(
        client,
        "tools/call",
        {"name": "gate", "arguments": {}, "inputResponses": {"confirm": answer}},
        name="gate",
    )
    body = await resp.json()
    assert body["result"]["content"][0]["text"] == "accepted"


@pytest.mark.parametrize(
    ("answer", "reported"),
    [
        ({"action": "decline"}, "decline"),
        ({"action": "cancel"}, "cancel"),
        ({"action": "something-else"}, "decline"),
    ],
)
async def test_refusals_are_not_agreement_and_stay_distinguishable(client, answer, reported):
    resp = await post26(
        client,
        "tools/call",
        {"name": "gate", "arguments": {}, "inputResponses": {"confirm": answer}},
        name="gate",
    )
    body = await resp.json()
    assert body["result"]["content"][0]["text"] == f"refused:{reported}"


async def test_ask_unwinds_then_resumes(client):
    first = await post26(
        client,
        "tools/call",
        {
            "name": "gated",
            "arguments": {},
            "_meta": {"io.modelcontextprotocol/clientCapabilities": {"elicitation": {}}},
        },
        name="gated",
    )
    body = await first.json()
    assert body["result"]["resultType"] == "input_required"
    assert "confirm" in body["result"]["inputRequests"]

    second = await post26(
        client,
        "tools/call",
        {
            "name": "gated",
            "arguments": {},
            "_meta": {"io.modelcontextprotocol/clientCapabilities": {"elicitation": {}}},
            "inputResponses": {"confirm": {"action": "accept", "content": {}}},
        },
        name="gated",
    )
    body = await second.json()
    assert body["result"]["content"][0]["text"] == "went ahead"


async def test_ask_reports_a_refusal(client):
    resp = await post26(
        client,
        "tools/call",
        {
            "name": "gated",
            "arguments": {},
            "_meta": {"io.modelcontextprotocol/clientCapabilities": {"elicitation": {}}},
            "inputResponses": {"confirm": {"action": "decline"}},
        },
        name="gated",
    )
    body = await resp.json()
    assert body["result"]["content"][0]["text"] == "stopped at decline"


async def test_ask_takes_one_round_per_question(client):
    """Each round must carry earlier answers when the handler restarts."""
    first = await post26(
        client,
        "tools/call",
        {
            "name": "twice",
            "arguments": {},
            "_meta": {"io.modelcontextprotocol/clientCapabilities": {"elicitation": {}}},
        },
        name="twice",
    )
    body = await first.json()
    assert list((await first.json())["result"]["inputRequests"]) == ["one"]

    second = await post26(
        client,
        "tools/call",
        {
            "name": "twice",
            "arguments": {},
            "_meta": {"io.modelcontextprotocol/clientCapabilities": {"elicitation": {}}},
            "inputResponses": {"one": {"action": "accept", "content": {"value": "a"}}},
        },
        name="twice",
    )
    body = await second.json()
    assert body["result"]["resultType"] == "input_required"
    assert list(body["result"]["inputRequests"]) == ["two"]

    third = await post26(
        client,
        "tools/call",
        {
            "name": "twice",
            "arguments": {},
            "_meta": {"io.modelcontextprotocol/clientCapabilities": {"elicitation": {}}},
            "inputResponses": {
                "one": {"action": "accept", "content": {"value": "a"}},
                "two": {"action": "accept", "content": {"value": "b"}},
            },
        },
        name="twice",
    )
    body = await third.json()
    assert body["result"]["content"][0]["text"] == "a+b"


async def test_ask_falls_back_to_the_default_where_asking_is_impossible(client):
    """Use the supplied default when the client cannot be asked."""
    resp = await post_legacy(client, "tools/call", {"name": "defaulted", "arguments": {}})
    body = await resp.json()
    assert body["result"]["content"][0]["text"] == "defaulted to decline"


async def test_ask_without_a_default_fails_honestly(client):
    resp = await post_legacy(client, "tools/call", {"name": "gated", "arguments": {}})
    body = await resp.json()
    assert body["error"]["code"] == -32603
