"""Ending a task that may not honour its first cancellation."""

from __future__ import annotations

import asyncio
from typing import Any

STOP_GRACE_SECONDS = 1.0


async def stop(*tasks: asyncio.Task[Any], grace: float = STOP_GRACE_SECONDS) -> None:
    """Cancel `tasks` and return when every one of them has ended.

    One cancellation is not always enough. A library may catch it, and on
    Python before 3.12 `asyncio.wait_for` drops it when the future it waits
    on completes in the same loop turn: psycopg_pool waits for a connection
    that way, and a sibling task that is cancelled first hands its
    connection back at exactly that moment. A task still running after
    `grace` seconds is cancelled again, at whatever it awaits by then.

    A cancelled task and one that ended with `ConnectionError` are what
    stopping looks like. Any other exception is raised, the first one found.
    """
    pending = {task for task in tasks if not task.done()}
    while pending:
        for task in pending:
            task.cancel()
        _, pending = await asyncio.wait(pending, timeout=grace)
    for task in tasks:
        if task.cancelled():
            continue
        error = task.exception()
        if error is not None and not isinstance(error, ConnectionError):
            raise error


__all__ = ["STOP_GRACE_SECONDS", "stop"]
