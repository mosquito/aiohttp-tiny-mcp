from __future__ import annotations

import uuid

import pytest

from aiohttp_tiny_mcp import Registry
from tests.storage.hub.helpers import Nothing, Watched, backlog


@pytest.fixture
def broadcast_registry() -> Registry:
    reg = Registry("broadcasting", "1.0", hub=Watched(), hub_poll_seconds=0.2)
    reg.extension(backlog())

    @reg.tool
    async def noop(args: Nothing) -> str:
        """Makes the server advertise tool list changes."""
        return ""

    return reg


@pytest.fixture
def hub(store_and_hub):
    return store_and_hub[1]


@pytest.fixture
def mine() -> str:
    return f"resume-{uuid.uuid4().hex[:12]}"
