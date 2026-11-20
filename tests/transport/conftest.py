import shutil
import subprocess
from pathlib import Path

import pytest
from aiohttp import web
from pydantic import BaseModel

from aiohttp_tiny_mcp import Exchange, MemoryHub, MemorySessionStore, Registry, SseEndpoint, elicit
from tests.helpers import OldClient


class Add(BaseModel):
    a: int
    b: int


class Nothing(BaseModel):
    pass


@pytest.fixture
def offered() -> Registry:
    registry = Registry("old", "1.0", hub=MemoryHub(), session_store=MemorySessionStore())

    @registry.tool
    async def add(args: Add) -> int:
        """Add two integers."""
        return args.a + args.b

    @registry.tool(streaming=True)
    async def counting(args: Nothing, ex: Exchange) -> str:
        """Reports before it answers."""
        await ex.progress(1, 2)
        await ex.log("warning", "halfway")
        return "counted"

    @registry.tool
    async def gated(args: Nothing, ex: Exchange) -> str:
        """Asks, on a revision two releases older than elicitation."""
        agreed = await ex.ask("confirm", elicit("Go ahead?"))
        return "went ahead" if agreed.accepted else "stopped"

    return registry


@pytest.fixture
async def served(offered):
    app = web.Application()
    SseEndpoint(offered).setup(app)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    host, port = runner.addresses[0]
    try:
        yield f"http://{host}:{port}"
    finally:
        await runner.cleanup()


@pytest.fixture
async def old(served):
    client = OldClient(served)
    await client.open()
    try:
        yield client
    finally:
        await client.close()


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


@pytest.fixture(params=[True, False], ids=["gzip", "plain"])
def enable_gzip(request) -> bool:
    """Run a stream test both compressed and not.

    The framing is the same either way, so anything that depends on the
    compressor is a defect rather than a mode.
    """
    return request.param
