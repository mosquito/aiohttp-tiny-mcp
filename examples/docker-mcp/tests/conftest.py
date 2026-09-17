"""Server fixtures with real aiodocker and SQLite against a fake HTTP daemon."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest
from aiodocker import Docker
from aiohttp import web
from aiohttp_tiny_mcp import Endpoint, Registry
from aiohttp_tiny_mcp.sqlite import SqliteHub, SqliteSessionStore, SqliteStorage
from fake_docker import FakeDocker

from docker_mcp.server import build


@pytest.fixture
def fake() -> FakeDocker:
    return FakeDocker()


@pytest.fixture
async def docker_url(fake: FakeDocker) -> AsyncIterator[str]:
    app = fake.app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    host, port = runner.addresses[0]
    try:
        yield f"http://{host}:{port}"
    finally:
        fake.live.put_nowait(None)
        await runner.cleanup()


@pytest.fixture
async def storage(tmp_path) -> AsyncIterator[SqliteStorage]:
    storage = SqliteStorage(tmp_path / "state.sqlite3")
    try:
        yield storage
    finally:
        await storage.close()


@pytest.fixture
async def client(docker_url: str) -> AsyncIterator[Docker]:
    """Real aiodocker client connected to the fake daemon."""
    client = Docker(url=docker_url)
    try:
        yield client
    finally:
        await client.close()


@pytest.fixture
def registry(storage: SqliteStorage, client: Docker) -> Registry:
    return build(
        Registry(
            "docker",
            "0.1.0",
            hub=SqliteHub(storage, look_again=0.01),
            session_store=SqliteSessionStore(storage),
        ),
        client,
    )


@pytest.fixture
async def url(registry: Registry) -> AsyncIterator[str]:
    runner = web.AppRunner(Endpoint(registry).app("/mcp"))
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    host, port = runner.addresses[0]
    try:
        yield f"http://{host}:{port}/mcp"
    finally:
        await runner.cleanup()
