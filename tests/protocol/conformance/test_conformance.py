"""docs/reference/verification.md: a fixed set of assertions every adapter must satisfy."""

from __future__ import annotations

import pytest

from aiohttp_tiny_mcp.core import FailureKind, Operation
from aiohttp_tiny_mcp.protocol.selection import AdapterSet
from aiohttp_tiny_mcp.protocol.v2026_07_28 import Adapter2026_07_28

ADAPTERS = AdapterSet.default().adapters


@pytest.mark.parametrize("adapter", ADAPTERS, ids=lambda a: a.version)
def test_method_map_round_trips(adapter):
    for operation in Operation:
        method = adapter.method_for(operation)
        if method is None:
            continue  # this revision doesn't expose the operation -- allowed
        assert adapter.operation_for(method) == operation


@pytest.mark.parametrize("adapter", ADAPTERS, ids=lambda a: a.version)
def test_failure_map_is_total(adapter):
    for kind in FailureKind:
        assert kind in adapter.FAILURE_MAP


@pytest.mark.parametrize("adapter", ADAPTERS, ids=lambda a: a.version)
def test_failure_map_yields_valid_status_codes(adapter):
    for kind in FailureKind:
        code, status = adapter.FAILURE_MAP[kind]
        assert isinstance(code, int)
        assert 100 <= status <= 599


def test_unknown_method_status_differs_between_eras():
    modern = AdapterSet.default().by_version["2026-07-28"]
    legacy = AdapterSet.default().by_version["2025-11-25"]
    assert modern.FAILURE_MAP[FailureKind.UNKNOWN_METHOD][1] == 404
    assert legacy.FAILURE_MAP[FailureKind.UNKNOWN_METHOD][1] == 200


def test_adapter_set_versions_equals_advertised_supported_versions():
    adapters = AdapterSet.default()
    modern = adapters.by_version["2026-07-28"]
    assert isinstance(modern, Adapter2026_07_28)
    assert tuple(modern.supported_versions) == adapters.versions


def test_batch_allowed_only_where_declared():
    for adapter in ADAPTERS:
        assert adapter.allows_batch == (adapter.version == "2025-03-26")
