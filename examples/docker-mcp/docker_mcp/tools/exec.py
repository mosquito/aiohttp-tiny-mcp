"""Running a command inside a container.

Three things this owes a caller that `docker exec` does not give for free. The
two streams are kept apart, because the daemon interleaves them and merging
loses which was which. The command is given a deadline, so a call cannot be
held open by something that never finishes. And a command that only reads runs
without asking anybody -- see `consent.py` for where that line is drawn.
"""

from __future__ import annotations

import asyncio

from aiodocker import Docker
from aiohttp_tiny_mcp import Exchange

from docker_mcp import daemon
from docker_mcp.consent import Policy, confirm, only_reads, refusal
from docker_mcp.errors import Conflict
from docker_mcp.models import ExecRequest, Executed

MAX_OUTPUT = 60_000


async def exec(args: ExecRequest, client: Docker, ex: Exchange, policy: Policy) -> Executed:
    """Run a command inside a running container, and report what it wrote.

    This is `docker exec`, not `docker run`: it needs a container that is
    already running. Use `create` to start a new one.

    A command that only reads -- `ls`, `cat`, `ps`, `df` and their like -- is
    run without asking at all. Anything else may ask first, because a command
    can do whatever the container can. It is given `seconds` to finish, and is
    reported as timed out after that.
    """
    found = await daemon.find(client, args.container)
    printable = " ".join(args.command)
    if not only_reads(args.command) and not await confirm(
        ex, policy, "exec", args.container, f"Run `{printable}` inside {args.container}?"
    ):
        return Executed(
            container=args.container,
            command=printable,
            exit_code=-1,
            stdout="",
            stderr=f"not run: {refusal(ex)}",
        )

    state = await daemon.state_of(found)
    if str(state.get("Status")) != "running":
        raise Conflict(
            f"{args.container} is {state.get('Status')}, and a command needs it running. "
            "Start it first, or use `read` to see a file without running anything."
        )

    session = await found.exec(
        args.command,
        stdout=True,
        stderr=True,
        workdir=args.workdir,
        user=args.user or "",
    )
    out: list[bytes] = []
    err: list[bytes] = []

    async def collect() -> None:
        async with session.start(detach=False) as stream:
            while (message := await stream.read_out()) is not None:
                (err if message.stream == 2 else out).append(message.data)

    timed_out = False
    try:
        await asyncio.wait_for(collect(), args.seconds)
    except asyncio.TimeoutError:
        timed_out = True

    result = {} if timed_out else await session.inspect()
    stdout = b"".join(out).decode("utf-8", "replace")
    stderr = b"".join(err).decode("utf-8", "replace")
    return Executed(
        container=args.container,
        command=printable,
        exit_code=-1 if timed_out else int(result.get("ExitCode") or 0),
        stdout=stdout[-MAX_OUTPUT:],
        stderr=stderr[-MAX_OUTPUT:],
        truncated=len(stdout) > MAX_OUTPUT or len(stderr) > MAX_OUTPUT,
        timed_out=timed_out,
    )
