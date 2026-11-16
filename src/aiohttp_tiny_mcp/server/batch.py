"""Bounded execution of batch items with results in input order."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from typing import TypeVar

from .tasks import stop

Item = TypeVar("Item")
Result = TypeVar("Result")


async def run_batch(
    items: Sequence[Item], execute: Callable[[Item], Awaitable[Result]], concurrency: int
) -> list[Result]:
    """Run at most concurrency items at once and finish all workers before returning."""
    if concurrency == 1 or len(items) <= 1:
        return [await execute(item) for item in items]
    pending = iter(enumerate(items))
    results: dict[int, Result] = {}

    async def worker() -> None:
        for index, item in pending:
            results[index] = await execute(item)

    workers = [
        asyncio.create_task(worker()) for worker_index in range(min(concurrency, len(items)))
    ]
    running = set(workers)
    try:
        while running:
            finished, running = await asyncio.wait(running, return_when=asyncio.FIRST_COMPLETED)
            for task in finished:
                task.result()
    finally:
        await stop(*workers)
    return [results[index] for index in range(len(items))]
