from collections.abc import AsyncIterator

import pytest
from aiohttp import web


@pytest.fixture
async def offered_over_sse(registry) -> AsyncIterator[str]:
    """The fixture registry on the transport 2024-11-05 speaks over HTTP."""
    from aiohttp_tiny_mcp import SseEndpoint

    app = web.Application()
    SseEndpoint(registry).setup(app)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    host, port = runner.addresses[0]
    try:
        yield f"http://{host}:{port}"
    finally:
        await runner.cleanup()
