"""Benchmark timing helpers."""

from __future__ import annotations

import asyncio
import statistics
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field


@dataclass
class Result:
    """What one measured run produced."""

    name: str
    calls: int
    concurrency: int
    seconds: float
    latencies: list[float] = field(default_factory=list)
    failed: int = 0

    @property
    def rate(self) -> float:
        """Calls a second, over the whole run."""
        return self.calls / self.seconds if self.seconds else 0.0

    def at(self, share: float) -> float:
        """One percentile of the latency, in milliseconds."""
        if not self.latencies:
            return 0.0
        ordered = sorted(self.latencies)
        index = min(int(len(ordered) * share), len(ordered) - 1)
        return ordered[index] * 1000

    @property
    def mean(self) -> float:
        return statistics.fmean(self.latencies) * 1000 if self.latencies else 0.0


async def measure(
    name: str,
    call: Callable[[], Awaitable[object]],
    *,
    calls: int,
    concurrency: int,
    warmup: int,
) -> Result:
    """Run `call` and report how long each one took.

    Warmed up first: the first calls of any client pay for a connection, a
    compiled schema and a cold branch predictor, and reporting those beside
    the steady state describes neither.
    """
    for _ in range(warmup):
        await call()

    latencies: list[float] = []
    failures = 0
    gate = asyncio.Semaphore(concurrency)

    async def once() -> None:
        nonlocal failures
        async with gate:
            started = time.perf_counter()
            try:
                await call()
            except Exception:  # noqa: BLE001 -- a failure is a number, not a stop
                failures += 1
            latencies.append(time.perf_counter() - started)

    started = time.perf_counter()
    await asyncio.gather(*[once() for _ in range(calls)])
    elapsed = time.perf_counter() - started
    return Result(name, calls, concurrency, elapsed, latencies, failures)


async def rounds(
    rows: list[tuple[str, Callable[[], Awaitable[object]]]],
    *,
    calls: int,
    concurrency: int,
    warmup: int,
    repeats: int,
) -> list[Result]:
    """Measure every row once, then go round again, and keep each row's best.

    Rotating rather than repeating one row at a time is what makes the run
    comparable with itself. A machine drifts -- another process wakes, a core
    parks, the collector runs -- and a row measured three times in a row wears
    whatever happened during its turn. Every row now meets the same weather.

    The best of the rounds is kept, not the mean, because interference only
    ever makes a measurement slower. The fastest round is the one that says
    most about the code and least about the machine.
    """
    best: dict[str, Result] = {}
    for _ in range(repeats):
        for name, call in rows:
            reached = await measure(name, call, calls=calls, concurrency=concurrency, warmup=warmup)
            if name not in best or reached.rate > best[name].rate:
                best[name] = reached
    return [best[name] for name, _ in rows]


def table(title: str, results: list[Result], *, floor: dict[str, float] | None = None) -> str:
    """One table, widest column first.

    `floor` names the bare framework each row runs on. Without it a reader
    compares two web servers and believes they compared two MCP libraries.
    """
    width = max([len(item.name) for item in results] + [len(title)]) + 2
    head = f"{title:<{width}} {'calls/s':>10} {'mean':>8} {'p50':>8} {'p95':>8} {'p99':>8}"
    if floor:
        head += f" {'of floor':>9}"
    rows = [head, "-" * len(head)]
    for item in results:
        row = (
            f"{item.name:<{width}} {item.rate:>10.0f} {item.mean:>7.2f}m"
            f" {item.at(0.5):>7.2f}m {item.at(0.95):>7.2f}m {item.at(0.99):>7.2f}m"
        )
        if floor:
            base = floor.get(item.name)
            row += f" {item.rate / base * 100:>8.0f}%" if base else f" {'':>9}"
        if item.failed:
            row += f"  ({item.failed} failed)"
        rows.append(row)
    return "\n".join(rows)
