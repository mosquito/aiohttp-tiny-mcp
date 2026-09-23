"""BaseClient transport over asyncio streams."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from typing import Any, Protocol, runtime_checkable

from aiohttp_tiny_mcp.protocol.adapter import Adapter
from aiohttp_tiny_mcp.protocol.models import Implementation

from .base import BaseClient, ClientError, Elicitor


@runtime_checkable
class ByteWriter(Protocol):
    """Writer interface required by StdioClient."""

    def write(self, data: bytes) -> Any: ...

    async def drain(self) -> Any: ...


class StdioClient(BaseClient):
    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: ByteWriter,
        adapter: Adapter,
        *,
        client_info: Implementation | None = None,
        on_ask: Elicitor | None = None,
        on_notification: Callable[[Mapping[str, Any]], Any] | None = None,
        log_level: str | None = None,
    ) -> None:
        super().__init__(
            adapter,
            client_info=client_info,
            on_ask=on_ask,
            on_notification=on_notification,
            log_level=log_level,
        )
        self.reader = reader
        self.writer = writer

    def write_line(self, envelope: dict[str, Any]) -> None:
        self.writer.write((json.dumps(envelope, ensure_ascii=False) + "\n").encode())

    async def exchange(
        self, envelope: dict[str, Any], *, method: str, name: str | None
    ) -> AsyncIterator[dict[str, Any]]:
        self.write_line(envelope)
        await self.writer.drain()
        while True:
            line = await self.reader.readline()
            if not line:
                raise ClientError(-32000, "stdio server closed the stream before replying")
            yield json.loads(line)

    async def send_notification(self, envelope: dict[str, Any], *, method: str) -> None:
        self.write_line(envelope)
        await self.writer.drain()

    async def reply(self, envelope: dict[str, Any]) -> None:
        """Send the answer on the shared JSON-RPC channel."""
        self.write_line(envelope)
        await self.writer.drain()

    @classmethod
    @asynccontextmanager
    async def spawn(
        cls,
        *cmd: str,
        adapter: Adapter,
        client_info: Implementation | None = None,
        on_ask: Elicitor | None = None,
        limit: int = 1024 * 1024,
    ) -> AsyncIterator[StdioClient]:
        """Launch cmd with protocol pipes and inherit stderr.

        `limit` bounds response lines in bytes and defaults to 1 MiB.
        """
        process = await asyncio.create_subprocess_exec(
            *cmd, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, limit=limit
        )
        assert process.stdin is not None
        assert process.stdout is not None
        try:
            yield cls(
                process.stdout, process.stdin, adapter, client_info=client_info, on_ask=on_ask
            )
        finally:
            process.terminate()
            await process.wait()
