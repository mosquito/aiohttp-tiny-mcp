"""Logging filters across session-based and per-request configuration."""

from __future__ import annotations

import pytest

from aiohttp_tiny_mcp import Client
from aiohttp_tiny_mcp.protocol.core import logs_at
from aiohttp_tiny_mcp.protocol.selection import AdapterSet

pytestmark = pytest.mark.asyncio

ADAPTERS = AdapterSet.default().adapters


async def test_a_client_that_asked_for_nothing_hears_nothing():
    assert logs_at(None, "emergency") is False
    assert logs_at("warning", "debug") is False
    assert logs_at("warning", "warning") is True
    assert logs_at("warning", "error") is True
    assert logs_at("debug", "debug") is True


async def test_an_unknown_severity_is_reported_rather_than_dropped():
    assert logs_at("warning", "trace") is True


def collect(seen):
    async def on_notification(frame):
        if frame.get("method") == "notifications/message":
            seen.append(frame["params"])

    return on_notification


@pytest.mark.parametrize("adapter", ADAPTERS, ids=lambda a: a.version)
async def test_a_level_set_once_governs_what_arrives(real_server_url, adapter):
    seen: list[dict] = []
    async with Client(
        real_server_url, adapter, log_level="warning", on_notification=collect(seen)
    ) as client:
        await client.initialize()
        await client.call_tool("noisy", {})
    assert [entry["level"] for entry in seen] == ["warning", "error"]
    assert seen[0]["logger"] == "noisy"
    assert seen[0]["data"] == "odd"


@pytest.mark.parametrize("adapter", ADAPTERS, ids=lambda a: a.version)
async def test_a_client_that_asked_for_nothing_is_told_nothing(real_server_url, adapter):
    seen: list[dict] = []
    async with Client(real_server_url, adapter, on_notification=collect(seen)) as client:
        await client.initialize()
        await client.call_tool("noisy", {})
    assert seen == []


@pytest.mark.parametrize("adapter", ADAPTERS, ids=lambda a: a.version)
async def test_the_level_can_be_changed_mid_session(real_server_url, adapter):
    seen: list[dict] = []
    async with Client(
        real_server_url, adapter, log_level="error", on_notification=collect(seen)
    ) as client:
        await client.initialize()
        await client.call_tool("noisy", {})
        assert [entry["level"] for entry in seen] == ["error"]
        seen.clear()
        await client.set_log_level("debug")
        await client.call_tool("noisy", {})
    assert [entry["level"] for entry in seen] == ["debug", "warning", "error"]


async def test_an_unknown_level_is_refused(real_server_url):
    """Only on a revision that sets it for the session; 2026-07-28 carries it
    in `_meta`, where nothing asks the server to agree first."""
    from aiohttp_tiny_mcp import ClientError

    adapter = AdapterSet.default().by_version["2025-11-25"]
    async with Client(real_server_url, adapter) as client:
        await client.initialize()
        with pytest.raises(ClientError):
            await client.set_log_level("chatty")
