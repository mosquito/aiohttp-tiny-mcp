"""`SSEResponse`, against the event-stream grammar it claims to follow."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import shutil
import subprocess
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from multidict import CIMultiDict, CIMultiDictProxy

from aiohttp_tiny_mcp.sse import Gone, SSEEvent, SSEResponse, read_sse


def test_one_line_of_data():
    assert str(SSEEvent("hello")) == "data: hello\n\n"


def test_data_is_split_over_one_field_per_line():
    """A `data` field holds one line, and a receiver joins several with newlines."""
    assert str(SSEEvent("a\nb")) == "data: a\ndata: b\n\n"


def test_every_line_ending_leaves_as_a_newline():
    assert str(SSEEvent("x\r\ny")) == str(SSEEvent("x\ny"))


def test_empty_data_is_still_an_event():
    """A `data:` with nothing after it is what primes a stream."""
    assert str(SSEEvent("")) == "data: \n\n"


def test_the_named_fields_come_before_the_data():
    assert str(SSEEvent("v", event="message", id="7")) == "event: message\nid: 7\ndata: v\n\n"


def test_a_retry_may_travel_alone():
    assert str(SSEEvent(retry=2000)) == "retry: 2000\n\n"


def test_a_comment_opens_with_a_colon():
    assert str(SSEEvent(comment="ping")) == ": ping\n\n"
    assert str(SSEEvent(comment="a\nb")) == ": a\n: b\n\n"


def test_an_event_is_frozen():
    """A queued event must be what it was when it was queued."""
    event = SSEEvent("v")
    with pytest.raises(dataclasses.FrozenInstanceError):
        event.data = "other"  # type: ignore[misc]


def test_to_bytes_is_the_string():
    event = SSEEvent("v", event="message")
    assert event.to_bytes() == str(event).encode()


@pytest.mark.parametrize("bad", ["two\nlines", "carriage\rreturn", "null\0byte"])
@pytest.mark.parametrize("field", ["event", "id"])
def test_a_field_that_would_break_the_framing_is_refused(field, bad):
    """Rewriting it silently would send something the caller did not ask to send."""
    with pytest.raises(ValueError, match=field):
        SSEEvent("v", **{field: bad})


def test_a_heartbeat_must_be_a_positive_interval():
    with pytest.raises(ValueError, match="positive"):
        SSEResponse(heartbeat=0)


class Chunks:
    """A response whose body arrives in the pieces given."""

    def __init__(self, *pieces: bytes) -> None:
        self.content = self
        self.pieces = pieces

    async def iter_any(self):
        for piece in self.pieces:
            yield piece


async def collect(*pieces: bytes, comments: bool = False) -> list[SSEEvent]:
    return [event async for event in read_sse(Chunks(*pieces), comments=comments)]


async def read_events(response, wanted: int, *, seconds: float = 5.0) -> list[SSEEvent]:
    """Read `wanted` events with the package's own reader."""
    events: list[SSEEvent] = []
    comments = 0
    stream = read_sse(response, comments=True)
    while len(events) < wanted:
        event = await asyncio.wait_for(stream.__anext__(), seconds)
        if event.comment is not None:
            comments += 1
        else:
            events.append(event)
    response.comments = comments
    return events


async def served(handler) -> AsyncIterator[TestClient]:
    app = web.Application()
    app.router.add_get("/stream", handler)
    async with TestClient(TestServer(app)) as client:
        yield client


@pytest.mark.timeout(1)
async def test_the_response_declares_itself_a_stream(enable_gzip):
    async def handler(request):
        stream = SSEResponse(heartbeat=None, compress=enable_gzip)
        await stream.prepare(request)
        await stream.send("hello")
        await stream.write_eof()
        return stream

    async for client in served(handler):
        response = await client.get("/stream")
        assert response.headers["Content-Type"] == "text/event-stream"
        assert response.headers["Cache-Control"] == "no-cache"
        assert response.headers["X-Accel-Buffering"] == "no"
        assert await read_events(response, 1) == [SSEEvent("hello")]


@pytest.mark.timeout(1)
async def test_json_goes_out_as_one_event(enable_gzip):
    async def handler(request):
        stream = SSEResponse(heartbeat=None, compress=enable_gzip)
        await stream.prepare(request)
        await stream.send_json({"jsonrpc": "2.0", "id": 1}, event="message", id="7")
        await stream.write_eof()
        return stream

    async for client in served(handler):
        response = await client.get("/stream")
        events = await read_events(response, 1)
    assert json.loads(events[0].data or "") == {"jsonrpc": "2.0", "id": 1}
    assert events[0].event == "message"
    assert events[0].id == "7"


@pytest.mark.timeout(1)
async def test_a_retry_is_sent_before_anything_else(enable_gzip):
    async def handler(request):
        stream = SSEResponse(heartbeat=None, retry=2500, compress=enable_gzip)
        await stream.prepare(request)
        await stream.send("first")
        await stream.write_eof()
        return stream

    async for client in served(handler):
        response = await client.get("/stream")
        body = await response.text()
    assert body.startswith("retry: 2500\n\n")


@pytest.mark.timeout(1)
async def test_a_quiet_stream_sends_keep_alive_comments(enable_gzip):
    """A proxy closes an idle connection, so silence has to carry traffic."""
    enough = asyncio.Event()

    async def handler(request):
        stream = SSEResponse(heartbeat=0.01, compress=enable_gzip)
        await stream.prepare(request)
        await enough.wait()
        await stream.send("late")
        await stream.write_eof()
        return stream

    seen = 0
    arrived = None
    async for client in served(handler):
        response = await client.get("/stream")
        async for piece in read_sse(response, comments=True):
            if piece.comment is not None:
                seen += 1
                if seen == 2:
                    enough.set()
                continue
            arrived = piece
            break
    assert arrived == SSEEvent("late")
    assert seen >= 2


@pytest.mark.timeout(1)
async def test_comments_never_land_inside_an_event(enable_gzip):
    """Writes are serialized, so a heartbeat cannot split a frame in two."""

    async def handler(request):
        stream = SSEResponse(heartbeat=0.001, compress=enable_gzip)
        await stream.prepare(request)
        for n in range(50):
            await stream.send_json({"n": n, "padding": "x" * 200})
        await stream.write_eof()
        return stream

    async for client in served(handler):
        response = await client.get("/stream")
        events = await read_events(response, 50)
    assert [json.loads(event.data or "")["n"] for event in events] == list(range(50))


@pytest.mark.timeout(1)
async def test_a_closed_stream_drops_writes_instead_of_raising(enable_gzip):
    """`close` ends the heartbeat, and anything sent afterwards goes nowhere.

    A dropped client is found by watching the connection, not by writing: a
    write to a client that has gone is buffered and fails only later.
    """

    async def handler(request):
        stream = SSEResponse(heartbeat=0.01, compress=enable_gzip)
        await stream.prepare(request)
        await stream.send("first")
        await stream.close()
        assert stream.closed is True
        assert stream.writer is None, "the writing task stops with the stream"
        await stream.send("never")
        await stream.comment("never")
        return stream

    async for client in served(handler):
        response = await client.get("/stream")
        body = await response.text()
    assert "never" not in body
    assert body.count("data: first") == 1


@pytest.mark.timeout(1)
async def test_a_finished_stream_reports_itself_closed(enable_gzip):
    async def handler(request):
        stream = SSEResponse(heartbeat=None, compress=enable_gzip)
        await stream.prepare(request)
        assert stream.closed is False
        await stream.write_eof()
        assert stream.closed is True
        return stream

    async for client in served(handler):
        assert (await client.get("/stream")).status == 200


READER = """
const url = process.argv[2];
const wanted = Number(process.argv[3]);
const seen = [];
const source = new EventSource(url);
const keep = (type) => (e) => {
  seen.push({ type, data: e.data, id: e.lastEventId });
  if (seen.length >= wanted) {
    source.close();
    process.stdout.write(JSON.stringify(seen));
    process.exit(0);
  }
};
source.addEventListener("message", keep("message"));
source.addEventListener("tick", keep("tick"));
setTimeout(() => { process.stderr.write("timed out\\n"); process.exit(1); }, 20000);
"""


PROBE = "process.exit(typeof EventSource === 'function' ? 0 : 1);\n"


@pytest.fixture
def node_bin() -> str:
    """The node binary, or a skipped test where there is none."""
    found = shutil.which("node")
    if found is None:
        pytest.skip("node is not installed")
    return found


@pytest.fixture
def eventsource_node(node_bin: str, tmp_path: Path) -> list[str]:
    """How to run a script under a node that has `EventSource`.

    It arrived behind `--experimental-eventsource` and is on by default in
    later releases, where the flag itself may be rejected.
    """
    probe = tmp_path / "probe.mjs"
    probe.write_text(PROBE, encoding="utf-8")
    for flags in ([], ["--experimental-eventsource"]):
        asked = subprocess.run([node_bin, *flags, str(probe)], capture_output=True)
        if asked.returncode == 0:
            return [node_bin, *flags]
    pytest.skip("this node has no EventSource")


@pytest.mark.timeout(10)
async def test_a_standard_eventsource_reads_what_this_writes(
    eventsource_node, tmp_path, enable_gzip
):
    """Our own reader agreeing with our own writer proves only that they agree.

    `EventSource` in node is undici's, written against the specification. What
    it makes of the stream is what a browser would make of it: the keep-alive
    comments disappear, a two-line `data` arrives as one string with a
    newline, a named event arrives under its name, and the id carries.
    """

    read = asyncio.Event()

    async def handler(request):
        stream = SSEResponse(heartbeat=0.02, compress=enable_gzip)
        await stream.prepare(request)
        await stream.send_json({"ok": True}, id="1")
        await stream.send("line one\nline two", id="2")
        await stream.send("counted", event="tick", id="3")
        await stream.send("after the quiet", id="4")
        await read.wait()
        return stream

    app = web.Application()
    app.router.add_get("/stream", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    try:
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        host, port = runner.addresses[0]

        script = tmp_path / "reader.mjs"
        script.write_text(READER, encoding="utf-8")
        reading = await asyncio.create_subprocess_exec(
            *eventsource_node,
            str(script),
            f"http://{host}:{port}/stream",
            "4",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await asyncio.wait_for(reading.communicate(), 30)
        read.set()
    finally:
        await runner.cleanup()

    assert reading.returncode == 0, err.decode()[:400]
    assert json.loads(out) == [
        {"type": "message", "data": '{"ok": true}', "id": "1"},
        {"type": "message", "data": "line one\nline two", "id": "2"},
        {"type": "tick", "data": "counted", "id": "3"},
        {"type": "message", "data": "after the quiet", "id": "4"},
    ]


@pytest.mark.timeout(1)
async def test_a_client_that_takes_gzip_gets_it():
    async def handler(request):
        stream = SSEResponse(heartbeat=None, compress=True)
        await stream.prepare(request)
        for n in range(20):
            await stream.send_json({"jsonrpc": "2.0", "id": n, "result": {"ok": True}})
        await stream.write_eof()
        return stream

    async for client in served(handler):
        response = await client.get("/stream", headers={"Accept-Encoding": "gzip"})
        assert response.headers["Content-Encoding"] == "gzip"
        events = await read_events(response, 20)
    assert [json.loads(event.data or "")["id"] for event in events] == list(range(20))


@pytest.mark.timeout(1)
async def test_a_client_that_does_not_take_gzip_gets_plain_text():
    async def handler(request):
        stream = SSEResponse(heartbeat=None, compress=True)
        await stream.prepare(request)
        await stream.send("plain")
        await stream.write_eof()
        return stream

    async for client in served(handler):
        response = await client.get("/stream", headers={"Accept-Encoding": "identity"})
        assert "Content-Encoding" not in response.headers
        assert await response.text() == "data: plain\n\n"


@pytest.mark.timeout(1)
async def test_compressed_events_arrive_before_the_stream_ends():
    """The point of the flush: without it the events sit in the compressor
    until enough of them accumulate, and a stream that waits delivers nothing."""

    read = asyncio.Event()

    async def handler(request):
        stream = SSEResponse(heartbeat=None, compress=True)
        await stream.prepare(request)
        await stream.send("first")
        await read.wait()  # open until the reader has it, which is the point
        return stream

    async for client in served(handler):
        response = await client.get("/stream", headers={"Accept-Encoding": "gzip"})
        events = await read_events(response, 1)
        read.set()
        response.close()
    assert events == [SSEEvent("first")]


@pytest.mark.timeout(1)
async def test_a_group_of_events_is_written_and_flushed_once():
    """Whatever is queued together compresses together, which is where a
    stream under load gets its ratio back."""
    written: list[int] = []

    async def handler(request):
        stream = SSEResponse(heartbeat=None, compress=True)
        await stream.prepare(request)
        real = stream.write

        async def counted(data: bytes) -> None:
            written.append(len(data))
            await real(data)

        stream.write = counted  # type: ignore[method-assign]
        for n in range(200):
            await stream.push(SSEEvent(f"n={n}"))
        await stream.write_eof()
        return stream

    async for client in served(handler):
        response = await client.get("/stream", headers={"Accept-Encoding": "gzip"})
        events = await read_events(response, 200, seconds=30)
    assert [event.data for event in events] == [f"n={n}" for n in range(200)]
    assert len(written) < 100, written


@pytest.mark.timeout(1)
async def test_a_repeated_header_is_not_collapsed(enable_gzip):
    """Headers are not a mapping: a caller may send one name twice."""
    sent = CIMultiDict([("Set-Cookie", "a=1"), ("Set-Cookie", "b=2")])

    async def handler(request):
        stream = SSEResponse(heartbeat=None, headers=sent, compress=enable_gzip)
        await stream.prepare(request)
        await stream.write_eof()
        return stream

    async for client in served(handler):
        response = await client.get("/stream")
        assert response.headers.getall("Set-Cookie") == ["a=1", "b=2"]
        assert response.headers["Content-Type"] == "text/event-stream"


@pytest.mark.timeout(1)
async def test_a_caller_may_replace_a_default_header(enable_gzip):
    async def handler(request):
        stream = SSEResponse(
            heartbeat=None, headers={"cache-control": "private, max-age=5"}, compress=enable_gzip
        )
        await stream.prepare(request)
        await stream.write_eof()
        return stream

    async for client in served(handler):
        response = await client.get("/stream")
        assert response.headers.getall("Cache-Control") == ["private, max-age=5"]


@pytest.mark.timeout(1)
async def test_a_subclass_may_replace_the_defaults(enable_gzip):
    class Plain(SSEResponse):
        default_headers = CIMultiDictProxy(CIMultiDict({"Content-Type": "text/event-stream"}))

    async def handler(request):
        stream = Plain(heartbeat=None, compress=enable_gzip)
        await stream.prepare(request)
        await stream.write_eof()
        return stream

    async for client in served(handler):
        response = await client.get("/stream")
        assert "X-Accel-Buffering" not in response.headers


@pytest.mark.parametrize("odd", ["a\u2028b", "a\u0085b", "a\x0bb", "a\x0cb"])
async def test_only_cr_and_lf_end_a_line(odd):
    """`splitlines` breaks on these too, which would put a line ending into
    data the sender never wrote."""
    assert str(SSEEvent(odd)) == f"data: {odd}\n\n"
    assert await collect(str(SSEEvent(odd)).encode()) == [SSEEvent(odd)]


async def test_data_keeps_a_trailing_line_break():
    """`splitlines` would drop it, and the reader would rebuild the wrong text."""
    assert str(SSEEvent("a\n")) == "data: a\ndata: \n\n"
    assert await collect(b"data: a\ndata: \n\n") == [SSEEvent("a\n")]


async def test_a_leading_byte_order_mark_is_stripped():
    """UTF-8 decoding strips one leading BOM, so the first field name is a
    field name and not something with an invisible character in front of it."""
    body = b"\xef\xbb\xbfdata: one\n\n"
    assert await collect(body) == [SSEEvent("one")]
    assert await collect(body[:1], body[1:2], body[2:]) == [SSEEvent("one")]


async def test_a_byte_order_mark_later_on_is_data():
    """Only a leading one is a mark; anywhere else it is a character."""
    assert await collect("data: a\ufeffb\n\n".encode()) == [SSEEvent("a\ufeffb")]


async def test_a_character_split_across_chunks_survives():
    """A chunk boundary falls where the transport put it. Frames are split
    before decoding, so a character cut in half is never decoded in half."""
    raw = "data: привет\n\n".encode()
    assert await collect(raw[:9], raw[9:]) == [SSEEvent("привет")]


async def test_a_line_break_split_across_chunks_is_one_break():
    """A CRLF cut in half must not read as two breaks, which would dispatch
    an event that has not ended."""
    assert await collect(b"data: one\r", b"\ndata: two\r\n\r\n") == [SSEEvent("one\ntwo")]


async def test_carriage_return_alone_ends_a_line():
    """The standard admits CR, LF and CRLF. Reading by lines would miss this one."""
    assert await collect(b"data: one\rdata: two\r\r") == [SSEEvent("one\ntwo")]


async def test_an_unfinished_event_is_dropped():
    """A stream that ends mid-event dispatches nothing."""
    assert await collect(b"data: half") == []


async def test_an_id_carries_to_later_events():
    events = await collect(b"id: 7\ndata: one\n\ndata: two\n\n")
    assert [(event.id, event.data) for event in events] == [("7", "one"), ("7", "two")]


@pytest.mark.timeout(1)
async def test_an_event_longer_than_a_read_buffer_arrives_whole(enable_gzip):
    """Data is one line, and a line here has no length a reader may assume.
    Reading by lines raises `LineTooLong` somewhere above 100 KB."""
    big = "x" * 2_000_000

    async def handler(request):
        stream = SSEResponse(heartbeat=None, compress=enable_gzip)
        await stream.prepare(request)
        await stream.send(big)
        await stream.write_eof()
        return stream

    async for client in served(handler):
        response = await client.get("/stream")
        events = await read_events(response, 1)
    assert events == [SSEEvent(big)]


def test_gone_is_a_connection_error():
    """So a caller already catching connection errors catches this one."""
    assert issubclass(Gone, ConnectionResetError)
    assert issubclass(Gone, ConnectionError)
    assert issubclass(Gone, OSError)


@pytest.mark.timeout(1)
async def test_aiohttp_compression_would_stall_a_live_stream():
    """Why this class compresses itself instead of calling `enable_compression`.

    `aiohttp` compresses each write but flushes only in `write_eof`, and its
    own `ZLibCompressor.compress` says so: "flush() must be called after the
    last call to compress()". A stream has no last call, so nothing reaches
    the client until it ends.

    If this ever starts passing, aiohttp learned to flush and the compressor
    here can go.
    """

    async def handler(request):
        stream = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        stream.enable_compression()
        await stream.prepare(request)
        await stream.write(str(SSEEvent("first")).encode())
        await waited.wait()
        return stream

    waited = asyncio.Event()
    async for client in served(handler):
        response = await client.get("/stream", headers={"Accept-Encoding": "gzip"})
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(response.content.readline(), 0.2)
        waited.set()
        response.close()


@pytest.mark.timeout(1)
async def test_a_large_compressed_event_survives_the_executor_path():
    """`ZLibCompressor` moves a big chunk to a thread. The flush that follows
    has to see the same compressor state."""
    big = "y" * 2_000_000

    read = asyncio.Event()

    async def handler(request):
        stream = SSEResponse(heartbeat=None, compress=True)
        await stream.prepare(request)
        await stream.send(big)
        await read.wait()
        return stream

    async for client in served(handler):
        response = await client.get("/stream", headers={"Accept-Encoding": "gzip"})
        events = await read_events(response, 1)
        read.set()
        response.close()
    assert events == [SSEEvent(big)]
