"""What a container is using, as a rate.

The counters are totals since it started, so one reading answers nothing. The
arithmetic between two of them lives in `docker.py`; what this file decides is
that a tool must pay for the second reading rather than return a number that
looks like a rate and is not.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

from aiodocker import Docker
from aiodocker.containers import DockerContainer

from docker_mcp import daemon
from docker_mcp.errors import Timeout, clear
from docker_mcp.models import ContainerRef, Stats

SAMPLE_SECONDS = 3.0


def megabytes(value: Any) -> float:
    return round(float(value or 0) / 1_000_000, 1)


def rate(now: Mapping[str, Any], before: Mapping[str, Any]) -> float:
    """Processor use as a percentage of one core, from two samples.

    A single sample cannot state a rate: the counters are totals since the
    container started. This is the arithmetic `docker stats` does, and it is
    why `stats` reads twice.
    """
    cpu = now.get("cpu_stats") or {}
    past = before.get("cpu_stats") or before.get("precpu_stats") or {}
    used = float((cpu.get("cpu_usage") or {}).get("total_usage", 0) or 0) - float(
        (past.get("cpu_usage") or {}).get("total_usage", 0) or 0
    )
    system = float(cpu.get("system_cpu_usage", 0) or 0) - float(
        past.get("system_cpu_usage", 0) or 0
    )
    cores = float(cpu.get("online_cpus", 0) or 0) or len(
        (cpu.get("cpu_usage") or {}).get("percpu_usage") or []
    )
    if used <= 0 or system <= 0:
        return 0.0
    return round(used / system * (cores or 1) * 100, 1)


def blocks(raw: Mapping[str, Any]) -> tuple[float, float]:
    """Bytes read from and written to disk. The daemon capitalises the
    operation one way on cgroup v1 and the other on v2."""
    entries = (raw.get("blkio_stats") or {}).get("io_service_bytes_recursive") or []

    def total(operation: str) -> float:
        return sum(
            float(item.get("value", 0) or 0)
            for item in entries
            if str(item.get("op", "")).lower() == operation
        )

    return megabytes(total("read")), megabytes(total("write"))


def stats_of(name: str, now: Mapping[str, Any], before: Mapping[str, Any]) -> Stats:
    memory = now.get("memory_stats") or {}
    used = float(memory.get("usage", 0) or 0)
    limit = float(memory.get("limit", 0) or 0)
    networks = (now.get("networks") or {}).values()
    read, written = blocks(now)
    return Stats(
        container=name,
        cpu_percent=rate(now, before),
        memory_mb=megabytes(used),
        memory_limit_mb=megabytes(limit),
        memory_percent=round(used / limit * 100, 1) if limit else 0.0,
        net_rx_mb=megabytes(sum(float(item.get("rx_bytes", 0) or 0) for item in networks)),
        net_tx_mb=megabytes(sum(float(item.get("tx_bytes", 0) or 0) for item in networks)),
        block_read_mb=read,
        block_write_mb=written,
        pids=int(((now.get("pids_stats") or {}).get("current")) or 0),
    )


async def samples(container: DockerContainer, name: str) -> Stats:
    """Read the counters twice and report the rate between. Give up rather than hold the call
    open, because a container that stops mid-read stops answering.
    """
    taken: list[Mapping[str, Any]] = []
    stream = container.stats(stream=True)

    async def collect() -> None:
        async for sample in stream:
            taken.append(sample)
            if len(taken) == 2:
                return

    async with clear("reading the counters", subject=name):
        try:
            await asyncio.wait_for(collect(), SAMPLE_SECONDS)
        except (asyncio.TimeoutError, StopAsyncIteration):
            pass
        finally:
            await stream.aclose()
    if not taken:
        raise Timeout(f"{name} reported no counters within {SAMPLE_SECONDS:.0f} seconds")
    if len(taken) == 1:
        return stats_of(name, taken[0], {"cpu_stats": taken[0].get("precpu_stats") or {}})
    return stats_of(name, taken[1], taken[0])


async def stats(args: ContainerRef, client: Docker) -> Stats:
    """What one container is using now: processor, memory, network and disk.

    Two readings a moment apart, because a rate cannot be had from one. Use
    this for "why is this machine busy" and for a container suspected of
    leaking memory -- read it twice, minutes apart, and compare.
    """
    found = await daemon.find(client, args.container)
    return await samples(found, args.container)
