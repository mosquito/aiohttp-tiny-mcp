import asyncio
from collections.abc import AsyncIterator
from textwrap import dedent

import pytest
import uvicorn

from tests.client.helpers import build_mcpserver_app


@pytest.fixture
async def mcp_sdk_server_url() -> AsyncIterator[str]:
    app = build_mcpserver_app()
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
    sock = config.bind_socket()
    port = sock.getsockname()[1]
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        while not server.started:
            await asyncio.sleep(0.01)
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        server.should_exit = True
        await task


@pytest.fixture
def catalogue_script(tmp_path):
    script = tmp_path / "catalogue.py"
    script.write_text(
        dedent("""\
        import asyncio
        from pydantic import BaseModel
        from aiohttp_tiny_mcp import Registry, run_stdio
        class Nothing(BaseModel):
            pass
        registry = Registry("catalogue", "1")
        async def tool(args: Nothing) -> str:
            return "ok"
        for index in range(26):
            registry.tool(tool, name=f"tool{index:02}", description="x" * 3000)
        asyncio.run(run_stdio(registry))
        """)
    )
    return str(script)
