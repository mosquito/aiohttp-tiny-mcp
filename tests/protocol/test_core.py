from __future__ import annotations

import json

from aiohttp_tiny_mcp.protocol.core import Preamble


def test_preamble_parses_header_and_meta_version():
    body = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"_meta": {"io.modelcontextprotocol/protocolVersion": "2026-07-28"}},
        }
    )
    pre = Preamble.of(body.encode(), {"MCP-Protocol-Version": "2026-07-28"})
    assert pre.method == "tools/call"
    assert pre.header_version == "2026-07-28"
    assert pre.meta_version == "2026-07-28"
    assert not pre.is_batch


def test_preamble_parses_initialize_version():
    body = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2025-03-26"},
        }
    )
    pre = Preamble.of(body.encode(), {})
    assert pre.initialize_version == "2025-03-26"
    assert pre.header_version is None


def test_preamble_unparseable_body_yields_none():
    pre = Preamble.of(b"not json", {})
    assert pre.body is None


def test_preamble_batch():
    body = json.dumps([{"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {}}])
    pre = Preamble.of(body.encode(), {})
    assert pre.is_batch
