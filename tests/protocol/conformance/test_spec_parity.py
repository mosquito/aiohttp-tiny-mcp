"""docs/reference/verification.md: every adapter's method map, against the spec types.

The source is the `mcp-types` distribution, which the dev group installs together with
`mcp`. It is a test dependency only: no module under `src` imports it.

A difference this package keeps is an entry in `NOT_DECODED`, with the reason. The
comparison uses set equality, so an entry that stops being a difference also fails. When
a raised dev pin fails a test here, adapt the package. Add an entry only for a difference
this package decided to keep.

The source is one implementation's view of the specification, not the specification. A
method the SDK adds before a revision publishes it arrives here as a failure.
"""

from __future__ import annotations

import pytest
from mcp_types import _v2025_11_25 as v2025_11_25_types
from mcp_types import _v2026_07_28 as v2026_07_28_types
from mcp_types.methods import (
    CLIENT_NOTIFICATIONS,
    CLIENT_REQUESTS,
    SERVER_NOTIFICATIONS,
    SERVER_REQUESTS,
)
from mcp_types.version import HANDSHAKE_PROTOCOL_VERSIONS, KNOWN_PROTOCOL_VERSIONS
from pydantic import BaseModel

from aiohttp_tiny_mcp import MemoryHub, MemorySessionStore, Registry
from aiohttp_tiny_mcp.protocol.adapter import Adapter
from aiohttp_tiny_mcp.protocol.core import Operation
from aiohttp_tiny_mcp.protocol.selection import AdapterSet
from aiohttp_tiny_mcp.protocol.v2025_11_25 import LEGACY_VERSIONS

ADAPTERS = AdapterSet.default().adapters

#: Inbound methods this package does not decode, and why. Each one is a client-to-server
#: method the source defines and no adapter maps to an `Operation`.
NOT_DECODED: dict[str, str] = {
    "notifications/cancelled": (
        "the transport handles it before the adapter: server/stdio.py matches pre.method "
        "and cancels the exchange"
    ),
    "notifications/progress": (
        "the server sends no progress token on the only request it makes, a pushed "
        "question, so a conforming client has nothing to report against; sampling "
        "would change that"
    ),
    "notifications/roots/list_changed": (
        "the server has no roots API; see verification.md, Not implemented"
    ),
}


def inbound(version: str) -> set[str]:
    """Return the client-to-server methods the source defines for `version`."""
    requests = {method for method, revision in CLIENT_REQUESTS if revision == version}
    notifications = {method for method, revision in CLIENT_NOTIFICATIONS if revision == version}
    return requests | notifications


def outbound(version: str) -> set[str]:
    """Return the server-to-client methods the source defines for `version`."""
    requests = {method for method, revision in SERVER_REQUESTS if revision == version}
    notifications = {method for method, revision in SERVER_NOTIFICATIONS if revision == version}
    return requests | notifications


def decoded(adapter: Adapter) -> set[str]:
    """Return the methods the adapter decodes, through its public map only."""
    return {adapter.method_for(operation) for operation in Operation} - {None}


@pytest.mark.parametrize("adapter", ADAPTERS, ids=lambda a: a.version)
def test_every_decoded_method_exists_in_the_revision(adapter):
    """A method this package accepts must be one the revision defines."""
    extra = decoded(adapter) - inbound(adapter.version)
    assert not extra, f"{adapter.version} decodes methods the source does not define: {extra}"


@pytest.mark.parametrize("adapter", ADAPTERS, ids=lambda a: a.version)
def test_undecoded_methods_are_declared_with_a_reason(adapter):
    """Every method the revision defines is decoded, or is a declared difference."""
    missing = inbound(adapter.version) - decoded(adapter)
    declared = set(NOT_DECODED) & inbound(adapter.version)
    assert missing == declared, (
        f"{adapter.version}: undeclared {missing - declared}, "
        f"declared but now decoded {declared - missing}. "
        f"Reasons: { {name: NOT_DECODED[name] for name in sorted(declared)} }"
    )


@pytest.mark.parametrize("adapter", ADAPTERS, ids=lambda a: a.version)
def test_no_server_to_client_method_in_the_map(adapter):
    """The map decodes requests. A method only the server sends has no place in it.

    `ping` is both, so the comparison uses the methods the revision gives to the server
    alone.
    """
    version = adapter.version
    server_only = outbound(version) - inbound(version)
    assert not decoded(adapter) & server_only


def test_revision_set_matches_the_source():
    """A revision the source adds must reach the adapters, or this fails first."""
    assert set(AdapterSet.default().versions) == set(KNOWN_PROTOCOL_VERSIONS)


def test_handshake_revisions_match_the_source():
    assert LEGACY_VERSIONS == frozenset(HANDSHAKE_PROTOCOL_VERSIONS)


class Nothing(BaseModel):
    pass


async def anything(args: Nothing) -> str:
    """Stands in for a handler, so the registry declares the capability."""
    return "ok"


async def completing(args: Nothing) -> list[str]:
    """Makes the registry declare `completions`."""
    return ["value"]


@pytest.fixture
def full_registry() -> Registry:
    """A registry that declares every capability this package can advertise."""
    registry = Registry("p", "1.0", hub=MemoryHub(), session_store=MemorySessionStore())
    registry.tool(anything)
    registry.resource("config://app", anything)
    registry.prompt(anything)
    registry.completions(completing)
    return registry


#: The source ships a capability model per revision for these two only. The older three
#: have the monolith model, which is a union across revisions and proves less.
CAPABILITY_MODELS = {
    "2025-11-25": v2025_11_25_types.ServerCapabilities,
    "2026-07-28": v2026_07_28_types.ServerCapabilities,
}


@pytest.mark.parametrize("version", sorted(CAPABILITY_MODELS))
def test_capability_names_and_subfields_match_the_source(version, full_registry):
    """Validate the advertised capabilities and read them back.

    The source model ignores a field it does not know, so an unknown capability or
    sub-field disappears in the round trip. Equality therefore checks both.
    """
    adapter = AdapterSet.default().by_version[version]
    declared = dict(adapter.capabilities(full_registry))
    model = CAPABILITY_MODELS[version]
    seen = model.model_validate(declared).model_dump(by_alias=True, exclude_none=True)
    assert seen == declared


def test_the_extensions_capability_is_a_field_of_2026_07_28():
    """`v2026_07_28` advertises `extensions` for a registry that has one."""
    model = CAPABILITY_MODELS["2026-07-28"]
    aliases = {field.alias or name for name, field in model.model_fields.items()}
    assert "extensions" in aliases
