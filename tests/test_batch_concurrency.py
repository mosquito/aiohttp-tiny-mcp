"""Batch concurrency preserves response envelopes and bounds active handlers."""

from __future__ import annotations

import asyncio
import gc
from collections.abc import AsyncIterator
from dataclasses import dataclass
from functools import partial
from typing import Any

import pytest
from aiohttp.test_utils import TestClient, TestServer
from pydantic import BaseModel

from aiohttp_tiny_mcp import Endpoint, Exchange, Registry, SseEndpoint
from aiohttp_tiny_mcp.auth import Authorization, Principal, StaticVerifier
from aiohttp_tiny_mcp.storage.namespaces import current

HEADERS = {"MCP-Protocol-Version": "2025-03-26"}
pytestmark = pytest.mark.timeout(10)


class Job(BaseModel):
    number: int


@dataclass
class Dependency:
    number: int
    namespace: str | None


def call(number: int, *, name: str = "job") -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": number,
        "method": "tools/call",
        "params": {"name": name, "arguments": {"number": number}},
    }


async def yield_once() -> None:
    ready = asyncio.get_running_loop().create_future()
    asyncio.get_running_loop().call_soon(ready.set_result, None)
    await ready


@pytest.mark.parametrize("endpoint_type", [Endpoint, SseEndpoint])
@pytest.mark.parametrize("value", [0, -1, True, False, 1.5, "2", None])
def test_batch_concurrency_requires_a_positive_integer(endpoint_type, value):
    with pytest.raises((TypeError, ValueError), match="batch_concurrency"):
        endpoint_type(Registry("batch", "1"), batch_concurrency=value)


async def test_explicit_one_executes_in_input_order():
    registry = Registry("batch", "1")
    events: list[tuple[str, int]] = []

    @registry.tool
    async def job(args: Job) -> str:
        events.append(("start", args.number))
        await yield_once()
        events.append(("finish", args.number))
        return str(args.number)

    endpoint = Endpoint(registry, batch_concurrency=1)
    async with TestClient(TestServer(endpoint.app())) as client:
        response = await client.post("/mcp", headers=HEADERS, json=[call(1), call(2), call(3)])
        assert response.status == 200
        body = await response.json()
        assert [entry["id"] for entry in body] == [1, 2, 3]
        assert all(not entry["result"].get("isError") for entry in body)
    assert events == [(state, number) for number in (1, 2, 3) for state in ("start", "finish")]


@pytest.mark.parametrize("concurrency", [None, 2, 4])
async def test_concurrent_batch_bounds_handlers_and_preserves_response_order(
    concurrency: int | None,
):
    registry = Registry("batch", "1")
    limit = 4 if concurrency is None else concurrency
    numbers = list(range(1, limit + 3))
    started: asyncio.Queue[int] = asyncio.Queue()
    finished: asyncio.Queue[int] = asyncio.Queue()
    gates = {number: asyncio.Event() for number in numbers}
    active = 0
    peak = 0

    @registry.tool
    async def job(args: Job) -> str:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        started.put_nowait(args.number)
        try:
            await gates[args.number].wait()
            finished.put_nowait(args.number)
            return str(args.number)
        finally:
            active -= 1

    endpoint = (
        Endpoint(registry)
        if concurrency is None
        else Endpoint(registry, batch_concurrency=concurrency)
    )
    async with TestClient(TestServer(endpoint.app())) as client:

        async def post():
            return await client.post("/mcp", headers=HEADERS, json=[call(i) for i in numbers])

        pending = asyncio.create_task(post())
        try:
            for number in range(1, limit + 1):
                assert await started.get() == number
            assert active == limit
            for number in numbers[1:]:
                gates[number].set()
                assert await finished.get() == number
                next_number = limit + number - 1
                if next_number in gates:
                    assert await started.get() == next_number
                    assert active == limit
            gates[1].set()
            assert await finished.get() == 1
            response = await pending
            assert response.status == 200
            body = await response.json()
            assert [entry["id"] for entry in body] == numbers
            assert [entry["result"]["content"][0]["text"] for entry in body] == [
                str(number) for number in numbers
            ]
            assert peak == limit
            assert active == 0
        finally:
            for gate in gates.values():
                gate.set()
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)


async def test_concurrency_limit_applies_to_each_batch():
    registry = Registry("batch", "1")
    started: asyncio.Queue[int] = asyncio.Queue()
    release = asyncio.Event()

    @registry.tool
    async def job(args: Job) -> str:
        started.put_nowait(args.number)
        await release.wait()
        return str(args.number)

    async with TestClient(TestServer(Endpoint(registry, batch_concurrency=2).app())) as client:

        async def post(first: int):
            return await client.post("/mcp", headers=HEADERS, json=[call(first), call(first + 1)])

        pending = [asyncio.create_task(post(first)) for first in (1, 3)]
        try:
            assert {await started.get() for i in range(4)} == {1, 2, 3, 4}
            release.set()
            responses = await asyncio.gather(*pending)
            assert all(response.status == 200 for response in responses)
        finally:
            release.set()
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)


async def test_concurrent_batch_keeps_errors_and_omits_notification_replies():
    registry = Registry("batch", "1")

    @registry.tool
    async def job(args: Job) -> str:
        await yield_once()
        if args.number == 2:
            raise ValueError("broken job")
        return str(args.number)

    payload = [
        call(1),
        call(2),
        {"jsonrpc": "2.0", "id": 3, "method": "missing", "params": {}},
        "invalid item",
        {"jsonrpc": "2.0", "method": "ping", "params": {}},
        call(4),
    ]
    async with TestClient(TestServer(Endpoint(registry, batch_concurrency=3).app())) as client:
        response = await client.post("/mcp", headers=HEADERS, json=payload)
        assert response.status == 200
        body = await response.json()
    assert [entry["id"] for entry in body] == [1, 2, 3, None, 4]
    assert body[0]["result"]["content"][0]["text"] == "1"
    assert body[1]["result"]["isError"]
    assert "broken job" in body[1]["result"]["content"][0]["text"]
    assert body[2]["error"]["code"] == -32601
    assert body[3]["error"]["code"] == -32600
    assert body[4]["result"]["content"][0]["text"] == "4"


async def test_notification_only_concurrent_batch_returns_no_body():
    async with TestClient(
        TestServer(Endpoint(Registry("batch", "1"), batch_concurrency=2).app())
    ) as client:
        notification = {"jsonrpc": "2.0", "method": "ping", "params": {}}
        response = await client.post("/mcp", headers=HEADERS, json=[notification, notification])
        assert response.status == 202
        assert await response.read() == b""


@pytest.mark.parametrize("version", ["2024-11-05", "2025-06-18", "2025-11-25", "2026-07-28"])
async def test_concurrency_does_not_enable_batches_on_other_revisions(version: str):
    async with TestClient(
        TestServer(Endpoint(Registry("batch", "1"), batch_concurrency=2).app())
    ) as client:
        response = await client.post(
            "/mcp",
            headers={"MCP-Protocol-Version": version},
            json=[{"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {}}],
        )
        assert response.status == 400
        assert (await response.json())["error"]["code"] == -32600


async def test_concurrency_keeps_single_request_response_as_an_object():
    async with TestClient(
        TestServer(Endpoint(Registry("batch", "1"), batch_concurrency=2).app())
    ) as client:
        response = await client.post(
            "/mcp",
            headers=HEADERS,
            json={"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {}},
        )
        assert response.status == 200
        assert await response.json() == {"jsonrpc": "2.0", "id": 1, "result": {}}


async def test_concurrent_batch_inherits_authentication_and_keeps_dependencies_per_call():
    registry = Registry(
        "batch",
        "1",
        auth=Authorization(
            verifier=StaticVerifier({"test-token": Principal(subject="alice", namespace="tenant")}),
            resource="https://example.test/mcp",
        ),
    )
    entered = 0
    ready = asyncio.Event()
    created: list[Dependency] = []
    closed: list[int] = []

    async def dependency(ex: Exchange) -> AsyncIterator[Dependency]:
        value = Dependency(ex.call.arguments["number"], current())
        created.append(value)
        try:
            yield value
        finally:
            closed.append(value.number)

    registry.provide(Dependency, dependency)

    @registry.tool
    async def job(args: Job, first: Dependency, second: Dependency, principal: Principal) -> str:
        nonlocal entered
        entered += 1
        if entered == 2:
            ready.set()
        await ready.wait()
        assert first is second
        assert first.number == args.number
        assert first.namespace == current()
        return f"{args.number}:{principal.subject}:{first.namespace}"

    async with TestClient(TestServer(Endpoint(registry, batch_concurrency=2).app())) as client:
        response = await client.post(
            "/mcp",
            headers={**HEADERS, "Authorization": "Bearer test-token"},
            json=[call(1), call(2)],
        )
        assert response.status == 200
        body = await response.json()
    assert [entry["result"]["content"][0]["text"] for entry in body] == [
        "1:alice:tenant",
        "2:alice:tenant",
    ]
    assert len(created) == 2 and created[0] is not created[1]
    assert sorted(closed) == [1, 2]


async def test_cancelled_batch_joins_running_workers_and_does_not_start_pending_items():
    from aiohttp_tiny_mcp.server.batch import run_batch

    started: asyncio.Queue[int] = asyncio.Queue()
    finished: set[int] = set()
    release = asyncio.Event()

    async def execute(number: int) -> int:
        started.put_nowait(number)
        try:
            await release.wait()
            return number
        finally:
            finished.add(number)

    pending = asyncio.create_task(run_batch(list(range(4)), execute, 2))
    assert await started.get() == 0
    assert await started.get() == 1
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert finished == {0, 1}
    assert started.empty()


async def test_cancelled_batch_retries_cancellation_when_a_worker_suppresses_it(monkeypatch):
    from aiohttp_tiny_mcp.server import batch
    from aiohttp_tiny_mcp.server.tasks import stop

    monkeypatch.setattr(batch, "stop", partial(stop, grace=0.01))
    started: asyncio.Queue[int] = asyncio.Queue()
    finished: set[int] = set()
    cancellations: list[int] = []
    release = asyncio.Event()

    async def execute(number: int) -> int:
        started.put_nowait(number)
        try:
            try:
                await release.wait()
            except asyncio.CancelledError:
                if number != 0:
                    raise
                cancellations.append(number)
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    cancellations.append(number)
                    raise
            return number
        finally:
            finished.add(number)

    pending = asyncio.create_task(batch.run_batch(list(range(4)), execute, 2))
    assert await started.get() == 0
    assert await started.get() == 1
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert cancellations == [0, 0]
    assert finished == {0, 1}
    assert started.empty()


async def test_cancelled_batch_does_not_report_background_exceptions():
    from aiohttp_tiny_mcp.server.batch import run_batch

    entered = asyncio.Event()
    errors: list[dict[str, Any]] = []
    loop = asyncio.get_running_loop()
    previous_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda loop, context: errors.append(context))

    async def execute(number: int) -> None:
        entered.set()
        await asyncio.Event().wait()

    pending = asyncio.create_task(run_batch([1, 2], execute, 2))
    try:
        await entered.wait()
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        await yield_once()
        gc.collect()
        await yield_once()
    finally:
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
        loop.set_exception_handler(previous_handler)
    assert errors == []
