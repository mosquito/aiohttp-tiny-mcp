"""One file out of a container, through the archive endpoint.

Not `cat`. The archive works on a stopped container, which is when a
configuration file is most worth reading, and it needs no shell inside the
image, which a slim image does not have.
"""

from __future__ import annotations

from aiodocker import Docker
from aiodocker.containers import DockerContainer

from docker_mcp import daemon
from docker_mcp.errors import Conflict, clear
from docker_mcp.models import FileContent, ReadRequest

MAX_FILE_BYTES = 256_000


async def read_file(container: DockerContainer, path: str) -> tuple[bytes, int, bool]:
    """Read one file as an archive: bytes, size, and whether it was cut.

    The archive endpoint works on a stopped container and needs no shell in the image.
    """
    async with clear("reading the file", subject=path):
        archive = await container.get_archive(path)
    members = archive.getmembers()
    if len(members) != 1 or not members[0].isfile():
        raise Conflict(f"{path} is a directory, not a file. Name a file inside it.")
    handle = archive.extractfile(members[0])
    content = handle.read(MAX_FILE_BYTES + 1) if handle is not None else b""
    size = int(members[0].size)
    return content[:MAX_FILE_BYTES], size, len(content) > MAX_FILE_BYTES


async def read(args: ReadRequest, client: Docker) -> FileContent:
    """Read one file out of a container.

    Works whether or not the container runs, and needs no shell inside it.
    Use it for configuration the environment does not show, and for a log the
    program wrote to a file instead of to its output.
    """
    found = await daemon.find(client, args.container)
    content, size, cut = await read_file(found, args.path)
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        return FileContent(
            container=args.container,
            path=args.path,
            size_bytes=size,
            text="",
            truncated=cut,
            binary=True,
        )
    return FileContent(
        container=args.container,
        path=args.path,
        size_bytes=size,
        text=text,
        truncated=cut,
    )
