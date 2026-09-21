from __future__ import annotations

import json

import pytest

from aiohttp_tiny_mcp.protocol.core import Preamble, Rejected
from aiohttp_tiny_mcp.protocol.selection import ASSUMED_VERSION, STABLE_REVISION, AdapterSet
from aiohttp_tiny_mcp.protocol.v2025_03_26 import Adapter2025_03_26
from aiohttp_tiny_mcp.protocol.v2025_06_18 import Adapter2025_06_18
from aiohttp_tiny_mcp.protocol.v2025_11_25 import Adapter2025_11_25
from aiohttp_tiny_mcp.protocol.v2026_07_28 import Adapter2026_07_28


def preamble(
    method: str,
    *,
    header_version: str | None = None,
    meta_version: str | None = None,
    query_version: str | None = None,
) -> Preamble:
    params = {}
    if meta_version is not None:
        params["_meta"] = {"io.modelcontextprotocol/protocolVersion": meta_version}
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    headers = {"MCP-Protocol-Version": header_version} if header_version else {}
    query = {"mcp": query_version} if query_version else {}
    return Preamble.of(body, headers, query)


@pytest.fixture
def adapters() -> AdapterSet:
    return AdapterSet.default()


def test_versions_newest_first(adapters):
    assert adapters.versions == (
        "2026-07-28",
        "2025-11-25",
        "2025-06-18",
        "2025-03-26",
        "2024-11-05",
    )


def test_select_by_header_version(adapters):
    picked = adapters.select(preamble("ping", header_version="2025-06-18"))
    assert picked.version == "2025-06-18"


def test_select_by_meta_version(adapters):
    picked = adapters.select(preamble("tools/call", meta_version="2026-07-28"))
    assert picked.version == "2026-07-28"


def test_conflicting_header_and_body_versions_are_rejected(adapters):
    with pytest.raises(Rejected):
        adapters.select(
            preamble("tools/list", header_version="2025-11-25", meta_version="2026-07-28")
        )


def test_non_string_body_version_is_rejected(adapters):
    body = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/list",
            "params": {"_meta": {"io.modelcontextprotocol/protocolVersion": []}},
        }
    ).encode()
    with pytest.raises(Rejected):
        adapters.select(Preamble.of(body, {}))


def test_select_unsupported_version_raises(adapters):
    with pytest.raises(Rejected):
        adapters.select(preamble("ping", header_version="1999-01-01"))


def test_select_by_query_pin(adapters):
    """A client that cannot send headers states its revision on the URL."""
    picked = adapters.select(preamble("ping", query_version="2025-06-18"))
    assert picked.version == "2025-06-18"


def test_query_pin_overrides_header_and_body(adapters):
    """The pin is set on the endpoint URL by whoever deployed it, so it wins
    over whatever the client itself claims -- that is what makes it usable to
    correct a client that negotiates badly."""
    picked = adapters.select(
        preamble(
            "tools/list",
            header_version="2026-07-28",
            meta_version="2026-07-28",
            query_version="2025-11-25",
        )
    )
    assert picked.version == "2025-11-25"


def test_query_pin_wins_even_when_header_and_body_disagree(adapters):
    """A conflict the pin resolves is not an error: the pin already said
    which revision this endpoint speaks."""
    picked = adapters.select(
        preamble(
            "tools/list",
            header_version="2025-11-25",
            meta_version="2026-07-28",
            query_version="2025-06-18",
        )
    )
    assert picked.version == "2025-06-18"


def test_unsupported_query_pin_raises(adapters):
    with pytest.raises(Rejected):
        adapters.select(preamble("ping", query_version="1999-01-01"))


def test_select_initialize_picks_the_stable_revision(adapters):
    picked = adapters.select(preamble("initialize"))
    assert picked.version == STABLE_REVISION


def test_stable_revision_selection_is_independent_of_adapter_order():
    """STABLE_REVISION is looked up explicitly, not by scanning `adapters` in
    constructor order -- reordering the list must not change the result."""
    reordered = AdapterSet(
        [Adapter2025_03_26(), Adapter2025_06_18(), Adapter2025_11_25(), Adapter2026_07_28()]
    )
    picked = reordered.select(preamble("initialize"))
    assert picked.version == STABLE_REVISION


def test_select_no_version_info_assumes_2025_03_26(adapters):
    picked = adapters.select(preamble("ping"))
    assert picked.version == ASSUMED_VERSION


def test_select_unparseable_body_uses_fallback(adapters):
    pre = Preamble.of(b"not json", {})
    picked = adapters.select(pre)
    assert picked is adapters.fallback()
    assert picked.version == "2026-07-28"


def test_2026_adapter_knows_all_supported_versions(adapters):
    modern = adapters.by_version["2026-07-28"]
    assert modern.supported_versions == adapters.versions
