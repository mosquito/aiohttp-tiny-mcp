"""Putting one file inside a container.

The daemon takes a tar archive extracted into a directory, so the smallest
write is still an archive of one member; `docker.write_file` builds it. Works
on a stopped container, which is what makes a broken configuration fixable
before the next start.
"""

from __future__ import annotations

import io
import tarfile
import time

from aiodocker import Docker
from aiodocker.containers import DockerContainer
from aiohttp_tiny_mcp import Exchange

from docker_mcp import daemon
from docker_mcp.consent import Policy, confirm, refusal
from docker_mcp.errors import Conflict, clear
from docker_mcp.models import WriteRequest, Written


async def write_file(container: DockerContainer, path: str, content: bytes, mode: int) -> None:
    """Write one file. The daemon extracts a tar archive into a directory, so the smallest write
    is an archive of one member.
    """
    directory, _, name = path.rpartition("/")
    if not name:
        raise Conflict(f"{path} names a directory, not a file")
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        entry = tarfile.TarInfo(name=name)
        entry.size = len(content)
        entry.mode = mode
        entry.mtime = int(time.time())
        archive.addfile(entry, io.BytesIO(content))
    async with clear("writing the file", subject=path):
        await container.put_archive(directory or "/", buffer.getvalue())


async def write(args: WriteRequest, client: Docker, ex: Exchange, policy: Policy) -> Written:
    """Write one file inside a container, creating or replacing it.

    Works on a stopped container, so a broken configuration can be corrected
    before starting it again. The file goes away with the container unless the
    path is on a volume, which is why the default policy does not ask before
    replacing it.
    """
    found = await daemon.find(client, args.container)
    if not await confirm(
        ex,
        policy,
        "write",
        args.container,
        f"Write {len(args.content)} bytes to {args.path} inside {args.container}? "
        "Whatever is there now is replaced.",
    ):
        raise Conflict(f"not written: {refusal(ex)}")
    content = args.content.encode("utf-8")
    await write_file(found, args.path, content, int(args.mode, 8))
    state = await daemon.state_of(found)
    note = None
    if str(state.get("Status")) == "running":
        note = "the container is running: restart it if it reads this file only at startup"
    return Written(container=args.container, path=args.path, size_bytes=len(content), note=note)
