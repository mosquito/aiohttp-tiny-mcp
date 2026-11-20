"""Round-trip state ids: call binding, namespace isolation, expiration, and single use."""

from __future__ import annotations

import pytest

from aiohttp_tiny_mcp import MemorySessionStore
from aiohttp_tiny_mcp.protocol.core import Call, ClientInfo, Operation
from aiohttp_tiny_mcp.protocol.models import Params
from aiohttp_tiny_mcp.server.request_state import RequestStates
from aiohttp_tiny_mcp.storage.namespaces import namespace

pytestmark = pytest.mark.asyncio


def call(arguments: dict | None = None, target: str = "deploy") -> Call:
    return Call(
        operation=Operation.CALL_TOOL,
        id=1,
        target=target,
        arguments=arguments or {"service": "web"},
        params=Params(),
        client=ClientInfo(),
    )


def states(ttl_seconds: int = 600) -> RequestStates:
    return RequestStates(MemorySessionStore(), ttl_seconds)


async def test_the_handler_reads_back_what_it_left():
    keep = states()
    handle = await keep.open(call(), {"step": 1})
    assert await keep.read(call(), handle) == {"step": 1}


async def test_an_id_is_bound_to_the_call_it_came_from():
    """State for delete #5 must not resume delete #9."""
    keep = states()
    handle = await keep.open(call({"service": "web"}), {"step": 1})
    with pytest.raises(KeyError):
        await keep.read(call({"service": "db"}), handle)
    with pytest.raises(KeyError):
        await keep.read(call(target="destroy"), handle)


async def test_an_id_is_unguessable():
    keep = states()
    handles = {await keep.open(call(), {"step": n}) for n in range(50)}
    assert len(handles) == 50
    assert all(len(handle) >= 32 for handle in handles)


async def test_an_id_reaches_nothing_in_another_namespace():
    keep = states()
    namespace.set("first")
    handle = await keep.open(call(), {"step": 1})
    namespace.set("second")
    try:
        with pytest.raises(KeyError):
            await keep.read(call(), handle)
    finally:
        namespace.set(None)


async def test_an_unknown_id_is_refused():
    keep = states()
    with pytest.raises(KeyError):
        await keep.read(call(), "never-issued")


async def test_a_dropped_id_reaches_nothing():
    keep = states()
    handle = await keep.open(call(), {"step": 1})
    await keep.drop(handle)
    with pytest.raises(KeyError):
        await keep.read(call(), handle)


async def test_a_state_id_is_not_a_session_id():
    """State and sessions share storage but must not accept each other's ids."""
    store = MemorySessionStore()
    keep = RequestStates(store)
    handle = await keep.open(call(), {"step": 1})
    assert await store.get(handle) is None
