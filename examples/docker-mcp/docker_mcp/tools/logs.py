"""What a container wrote, dated, and readable a second time.

Two things are worth knowing here. The daemon can deliver both streams at
once but not labelled, so they are read separately and merged by time -- which
is the only reason a line knows whether it was a report or a failure. And
`cursor` is what makes a second read cheap: without it a caller asks for the
same tail again and cannot tell what is new.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from aiodocker import Docker
from aiodocker.containers import DockerContainer

from docker_mcp import daemon
from docker_mcp.errors import clear
from docker_mcp.models import LogLine, LogRequest, Logs, Stream

UNDATED = "~"

OVERSHOOT = 4

MOST_LINES = 5000


def timestamped(text: str, stream: Stream) -> LogLine:
    """One line as Docker writes it with `timestamps=1`: a time, a space, the
    line. A line without one is kept, because a log is worth reading even
    where the daemon did not date it."""
    head, _, rest = text.partition(" ")
    if head.count("-") == 2 and head.count(":") >= 2:
        return LogLine(at=head, stream=stream, text=rest)
    return LogLine(at=None, stream=stream, text=text)


def as_epoch(text: str) -> str:
    """One log timestamp, in the seconds `since` is actually counted in.

    The daemon writes RFC 3339 with nanoseconds and reads back only a Unix
    timestamp -- `since=2026-09-13T20:30:42.897161158Z` is refused as an
    invalid number of seconds. The nanoseconds are carried across as the
    fraction, because a cursor rounded to the second repeats a whole second
    of the log on every read.
    """
    body = text.strip().rstrip("Z")
    if not body or body.replace(".", "", 1).isdigit():
        return body or "0"
    head, _, fraction = body.partition(".")
    moment = datetime.strptime(head, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    seconds = int(moment.timestamp())
    return f"{seconds}.{fraction}" if fraction else str(seconds)


async def log_lines(
    container: DockerContainer, *, stream: Stream, lines: int, since: str | None
) -> list[LogLine]:
    """Read the tail of one stream, dated. The daemon delivers both streams together but
    unlabelled, so each is read separately and the caller merges them.
    """
    params: dict[str, Any] = {"timestamps": True, "tail": lines}
    if since:
        params["since"] = as_epoch(since)
    async with clear("reading the log"):
        raw = await container.log(stdout=stream == "stdout", stderr=stream == "stderr", **params)
    written = raw if isinstance(raw, list) else str(raw).splitlines()
    return [timestamped(str(line).rstrip("\n"), stream) for line in written if str(line).strip()]


async def logs(args: LogRequest, client: Docker) -> Logs:
    """Read what a container wrote, newest last.

    The first place to look when something failed. Every line is dated, and
    `cursor` comes back with the time of the last one -- pass it as `since` on
    the next call to read only what is new, instead of the same tail again.
    Narrow a noisy log with `contains` rather than by asking for more lines.
    """
    found = await daemon.find(client, args.container)
    wanted: list[Stream] = ["stdout", "stderr"] if args.stream == "both" else [args.stream]
    asked = args.lines if len(wanted) == 2 else min(args.lines * OVERSHOOT, MOST_LINES)
    collected = []
    for stream in wanted:
        collected += await log_lines(found, stream=stream, lines=asked, since=args.since)
    fetched = len(collected)
    collected.sort(key=lambda line: line.at or UNDATED)
    if args.since:
        collected = [line for line in collected if (line.at or "") > args.since]
    if args.contains:
        needle = args.contains.lower()
        collected = [line for line in collected if needle in line.text.lower()]
    kept = collected[-args.lines :]
    tagged = args.stream == "both"
    return Logs(
        container=args.container,
        lines=[line.rendered(tagged=tagged) for line in kept],
        shown=len(kept),
        truncated=fetched >= asked or len(collected) > len(kept),
        cursor=next((line.at for line in reversed(kept) if line.at), None),
    )
