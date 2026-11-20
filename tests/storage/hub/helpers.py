from __future__ import annotations

import asyncio

from pydantic import BaseModel

from aiohttp_tiny_mcp import Extension, MemoryHub
from aiohttp_tiny_mcp.storage.hub import NOTIFICATIONS, topic

TASK = "notifications/backlog/task"
NOTE = "notifications/backlog/note"


class Nothing(BaseModel):
    pass


class Watched(MemoryHub):
    """Counts the readers that took a starting position on the notification topic."""

    def __init__(self) -> None:
        super().__init__()
        self.readers = 0
        self.changed = asyncio.Event()

    async def position(self, where: str) -> str:
        found = await super().position(where)
        if where == topic(NOTIFICATIONS):
            self.readers += 1
            self.changed.set()
        return found

    async def readers_reach(self, count: int) -> None:
        while self.readers < count:
            self.changed.clear()
            await self.changed.wait()


def backlog(*notifications: str) -> Extension:
    extension = Extension("example.org/backlog", notifications=notifications or (TASK, NOTE))

    @extension.method("backlog/ping")
    async def ping(args: Nothing) -> dict:
        return {"ok": True}

    return extension
