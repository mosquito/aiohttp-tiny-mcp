"""SSE encoding, decoding, and responses."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, replace
from typing import Any, ClassVar
from zlib import Z_SYNC_FLUSH

import aiohttp
from aiohttp import web
from aiohttp.compression_utils import ZLibCompressor
from multidict import CIMultiDict, CIMultiDictProxy

BOM = b"\xef\xbb\xbf"


class Gone(ConnectionResetError):
    """The SSE client disconnected."""


def one_line(value: str, field: str) -> str:
    """Refuse a value that would start a line the receiver reads as a field."""
    if "\n" in value or "\r" in value or "\0" in value:
        raise ValueError(f"an SSE {field} cannot contain a newline or a null")
    return value


def lines(prefix: str, text: str) -> list[str]:
    """Prefix each CR, LF, or CRLF-delimited line."""
    flat = text.replace("\r\n", "\n").replace("\r", "\n")
    return [f"{prefix} {part}" for part in flat.split("\n")]


@dataclass(frozen=True, slots=True)
class SSEEvent:
    """One event-stream frame."""

    data: str | None = None
    event: str | None = None
    id: str | None = None
    retry: int | None = None
    comment: str | None = None

    def __post_init__(self) -> None:
        for field in ("event", "id"):
            value = getattr(self, field)
            if value is not None:
                one_line(value, field)

    def __str__(self) -> str:
        out: list[str] = []
        if self.comment is not None:
            out += lines(":", self.comment)
        if self.event is not None:
            out.append(f"event: {self.event}")
        if self.id is not None:
            out.append(f"id: {self.id}")
        if self.retry is not None:
            out.append(f"retry: {int(self.retry)}")
        if self.data is not None:
            out += lines("data:", self.data)
        return "".join(f"{line}\n" for line in out) + "\n"

    def to_bytes(self) -> bytes:
        return str(self).encode()


async def read_frames(response: aiohttp.ClientResponse) -> AsyncIterator[bytes]:
    """Yield raw SSE frames."""
    buffer = b""
    held = b""
    start = True
    chunks = response.content.iter_any()
    ended = False
    while not ended:
        try:
            chunk = held + await chunks.__anext__()
            held = b""
            if chunk.endswith(b"\r"):
                chunk, held = chunk[:-1], b"\r"
        except StopAsyncIteration:
            chunk, held, ended = held, b"", True
        buffer += chunk.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
        if start and (len(buffer) >= len(BOM) or ended):
            buffer = buffer.removeprefix(BOM)
            start = False
        while (cut := buffer.find(b"\n\n")) >= 0:
            yield buffer[:cut]
            buffer = buffer[cut + 2 :]


async def read_sse(
    response: aiohttp.ClientResponse, *, comments: bool = False
) -> AsyncIterator[SSEEvent]:
    """Decode events from an SSE response."""
    last_id: str | None = None
    async for frame in read_frames(response):
        held = SSEEvent(id=last_id)
        data: list[str] = []
        for line in frame.decode("utf-8", "replace").split("\n"):
            field, _, value = line.partition(":")
            if value.startswith(" "):
                value = value[1:]
            if not field:
                if comments and line:
                    yield SSEEvent(comment=value)
            elif field == "data":
                data.append(value)
            elif field == "event":
                held = replace(held, event=value)
            elif field == "id" and "\0" not in value:
                held = replace(held, id=value)
            elif field == "retry" and value.isdigit():
                held = replace(held, retry=int(value))
        last_id = held.id
        if data:
            yield replace(held, data="\n".join(data))


class SSEResponse(web.StreamResponse):
    """A queued `text/event-stream` response."""

    default_headers: ClassVar[Mapping[str, str]] = CIMultiDictProxy(
        CIMultiDict(
            {
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",  # nginx must not buffer this
            }
        )
    )

    PING: ClassVar[SSEEvent] = SSEEvent(comment="ping")

    HEARTBEAT_SECONDS: ClassVar[float] = 15.0

    MAX_QUEUE: ClassVar[int] = 1024

    FLUSH_SECONDS: ClassVar[float] = 0.02

    def __init__(
        self,
        *,
        heartbeat: float | None = HEARTBEAT_SECONDS,
        retry: int | None = None,
        max_queue: int = MAX_QUEUE,
        compress: bool = True,
        headers: Mapping[str, str] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        if heartbeat is not None and heartbeat <= 0:
            raise ValueError(f"heartbeat must be positive seconds, got {heartbeat!r}")
        given = CIMultiDict(headers or {})
        for name, value in self.default_headers.items():
            if name not in given:
                self.headers[name] = value
        self.headers.extend(given)
        self.heartbeat = heartbeat
        self.retry = retry
        self.compress = compress
        self.queue: asyncio.Queue[SSEEvent | None] = asyncio.Queue(maxsize=max_queue)
        self.writer: asyncio.Task[None] | None = None
        self.gzip: ZLibCompressor | None = None

    @property
    def closed(self) -> bool:
        """Never opened, closed, or the write failed."""
        return self.writer is None or self.writer.done()

    async def prepare(self, request: web.BaseRequest) -> Any:
        if self.compress and "gzip" in request.headers.get("Accept-Encoding", ""):
            self.headers["Content-Encoding"] = "gzip"
            self.gzip = ZLibCompressor(encoding="gzip")
        written = await super().prepare(request)
        if self.writer is None:
            self.writer = asyncio.create_task(self._deliver())
        if self.retry is not None:
            await self.push(SSEEvent(retry=self.retry))
        return written

    async def send(self, data: str, *, event: str | None = None, id: str | None = None) -> None:
        await self.push(SSEEvent(data, event=event, id=id))

    async def send_json(
        self, value: Any, *, event: str | None = None, id: str | None = None
    ) -> None:
        await self.send(json.dumps(value, ensure_ascii=False), event=event, id=id)

    async def comment(self, text: str = "") -> None:
        await self.push(SSEEvent(comment=text))

    async def push(self, event: SSEEvent) -> None:
        """Queue one event, waiting where the queue is full."""
        if not self.closed:
            await self.queue.put(event)

    async def _deliver(self) -> None:
        """Write the queue until it ends or the client stops reading."""
        try:
            while True:
                batch: list[SSEEvent | None] = []
                try:
                    batch.append(await self.take(self.heartbeat))
                except asyncio.TimeoutError:
                    batch.append(self.PING)
                while not self.queue.empty():
                    batch.append(self.queue.get_nowait())
                ending = None in batch
                await self._emit(b"".join([await self._encode(e) for e in batch if e]))
                if self.gzip is not None:
                    await self._emit(self.gzip.flush() if ending else self.gzip.flush(Z_SYNC_FLUSH))
                if ending:
                    return
        except Gone:
            return
        finally:
            while not self.queue.empty():
                self.queue.get_nowait()

    async def take(self, waiting: float | None) -> SSEEvent | None:
        if waiting is None:
            return await self.queue.get()
        return await asyncio.wait_for(self.queue.get(), waiting)

    async def _encode(self, event: SSEEvent) -> bytes:
        """The bytes for one event. Large ones compress off the event loop."""
        raw = event.to_bytes()
        return await self.gzip.compress(raw) if self.gzip is not None else raw

    async def _emit(self, data: bytes) -> None:
        if not data:
            return
        try:
            await self.write(data)
        except (ConnectionError, RuntimeError):
            raise Gone from None

    async def close(self) -> None:
        """Write what is queued, then stop."""
        writer, self.writer = self.writer, None
        if writer is not None and not writer.done():
            await self.queue.put(None)
            await writer

    async def write_eof(self, data: bytes = b"") -> None:
        await self.close()
        await super().write_eof(data)


__all__ = [
    "Gone",
    "SSEEvent",
    "SSEResponse",
    "read_frames",
    "read_sse",
]
