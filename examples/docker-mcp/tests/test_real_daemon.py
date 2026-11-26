"""Docker integration tests, skipped when the daemon is unavailable.

DOCKER_MCP_DOCKER_URL=unix:///var/run/docker.sock pytest -k real
"""

from __future__ import annotations

import os
from uuid import uuid4

import pytest
from aiodocker import Docker
from aiodocker.exceptions import DockerError
from aiohttp_tiny_mcp import (
    Client,
    Endpoint,
    MemoryHub,
    MemorySessionStore,
    Registry,
)
from aiohttp_tiny_mcp.protocol.selection import AdapterSet

from docker_mcp import tools
from docker_mcp.models import ContainerFilter, ImageFilter, Nothing
from docker_mcp.server import build

pytestmark = pytest.mark.asyncio

MODERN = AdapterSet.default().by_version["2026-07-28"]


async def reachable(client: Docker) -> bool:
    try:
        await client.version()
    except Exception:  # noqa: BLE001 -- any failure means "not there"
        return False
    return True


@pytest.fixture
async def real_daemon():
    url = os.environ.get("DOCKER_MCP_DOCKER_URL")
    client = Docker(url=url) if url else Docker()
    if not await reachable(client):
        await client.close()
        pytest.skip("no Docker daemon reachable")
    try:
        yield client
    finally:
        await client.close()


async def test_real_daemon_reports_itself(real_daemon):
    reported = await tools.info(Nothing(), real_daemon)
    assert reported.version
    assert reported.api_version
    assert reported.containers_total >= 0


async def test_real_containers_reshape_without_losing_anything(real_daemon):
    listed = await tools.containers(ContainerFilter(all=True, limit=500), real_daemon)
    for item in listed.containers:
        assert item.id and len(item.id) == 12
        assert item.name
        assert item.state


async def test_real_images_reshape(real_daemon):
    listed = await tools.images(ImageFilter(), real_daemon)
    for item in listed.images:
        assert item.size_mb >= 0


async def test_a_real_server_answers_a_real_client(real_daemon):
    from aiohttp import web

    registry = build(
        Registry("docker", "0.1.0", hub=MemoryHub(), session_store=MemorySessionStore()),
        real_daemon,
    )
    runner = web.AppRunner(Endpoint(registry).app("/mcp"))
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    host, port = runner.addresses[0]
    try:
        async with Client(f"http://{host}:{port}/mcp", MODERN) as client:
            await client.initialize()
            result = await client.call_tool("info", {})
    finally:
        await runner.cleanup()

    assert result.structured_content["version"]


async def test_a_real_container_can_be_lived_with_end_to_end(real_daemon):
    from aiohttp import web
    from aiohttp_tiny_mcp import elicit_accept

    async def agree(request):
        return elicit_accept({"confirmed": True, "remember": "any"})

    registry = build(
        Registry("docker", "0.1.0", hub=MemoryHub(), session_store=MemorySessionStore()),
        real_daemon,
    )
    runner = web.AppRunner(Endpoint(registry).app("/mcp"))
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    host, port = runner.addresses[0]
    name = "docker-mcp-selftest"
    try:
        async with Client(f"http://{host}:{port}/mcp", MODERN, on_ask=agree) as client:
            await client.initialize()
            await client.call_tool("remove", {"container": name, "force": True})

            made = await client.call_tool(
                "create",
                {
                    "image": "alpine:3.20",
                    "name": name,
                    "command": ["sh", "-c", "echo ready; while true; do sleep 1; done"],
                },
            )
            assert made.is_error is False, made.content[0].text

            waited = await client.call_tool(
                "wait", {"container": name, "state": "running", "seconds": 20}
            )
            assert waited.structured_content["state"] == "running"

            ran = await client.call_tool("exec", {"container": name, "command": ["ls", "/etc"]})
            assert ran.structured_content["exit_code"] == 0
            assert ran.structured_content["stdout"]

            await client.call_tool(
                "write", {"container": name, "path": "/tmp/note.txt", "content": "written\n"}
            )
            read = await client.call_tool("read", {"container": name, "path": "/tmp/note.txt"})
            assert read.structured_content["text"] == "written\n"

            written = await client.call_tool("logs", {"container": name})
            assert any("ready" in line for line in written.structured_content["lines"])
            cursor = written.structured_content["cursor"]
            again = await client.call_tool("logs", {"container": name, "since": cursor})
            assert again.structured_content["lines"] == []

            counted = await client.call_tool("stats", {"container": name})
            assert counted.structured_content["memory_mb"] > 0

            happened = await client.call_tool("events", {"container": name, "seconds": 120})
            assert any(item["action"] == "start" for item in happened.structured_content["events"])

            stopped = await client.call_tool("stop", {"container": name, "seconds": 1})
            assert stopped.structured_content["state"] == "exited"

            # A file survives the container stopping, and is still readable.
            after = await client.call_tool("read", {"container": name, "path": "/tmp/note.txt"})
            assert after.structured_content["text"] == "written\n"

            gone = await client.call_tool("remove", {"container": name})
            assert gone.content[0].text == f"removed {name}"
    finally:
        await runner.cleanup()


async def test_real_unprivileged_volume_survives_container_replacement(real_daemon):
    from aiohttp import web

    from docker_mcp.consent import Policy

    name = f"docker-mcp-restricted-{uuid4().hex}"
    volume_name = f"{name}-data"
    network = await real_daemon.networks.create({"Name": name, "Driver": "bridge"})
    registry = build(
        Registry("docker", "0.1.0"),
        real_daemon,
        Policy("never"),
        unprivileged=True,
        network=name,
    )
    runner = web.AppRunner(Endpoint(registry).app("/mcp"))
    created = []
    try:
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        host, port = runner.addresses[0]
        async with Client(f"http://{host}:{port}/mcp", MODERN) as client:
            await client.initialize()
            offered = {tool.name for tool in await client.list_tools()}
            assert "networks" not in offered
            for index in range(2):
                container_name = f"{name}-{index}"
                made = await client.call_tool(
                    "create",
                    {
                        "image": "alpine:3.20",
                        "name": container_name,
                        "command": ["sleep", "60"],
                        "volumes": [f"{volume_name}:/data"],
                    },
                )
                created.append(container_name)
                assert not made.is_error, made.content
                if index == 0:
                    written = await client.call_tool(
                        "write",
                        {"container": container_name, "path": "/data/note", "content": "persisted"},
                    )
                    assert not written.is_error, written.content
                read = await client.call_tool(
                    "read", {"container": container_name, "path": "/data/note"}
                )
                assert not read.is_error, read.content
                assert read.structured_content["text"] == "persisted"
                executed = await client.call_tool(
                    "exec", {"container": container_name, "command": ["cat", "/data/note"]}
                )
                assert not executed.is_error, executed.content
                assert executed.structured_content["stdout"] == "persisted"
                for action in ("stop", "start", "restart"):
                    arguments: dict[str, str | int] = {"container": container_name}
                    if action != "start":
                        arguments["seconds"] = 0
                    result = await client.call_tool(action, arguments)
                    assert not result.is_error, result.content
                removed = await client.call_tool(
                    "remove", {"container": container_name, "force": True, "volumes": True}
                )
                assert not removed.is_error, removed.content
                created.remove(container_name)
    finally:
        for container_name in created:
            try:
                container = await real_daemon.containers.get(container_name)
            except DockerError as error:
                if error.status == 404:
                    continue
                raise
            await container.delete(force=True, v=True)
        volumes = await real_daemon.volumes.list()
        if any(volume["Name"] == volume_name for volume in volumes.get("Volumes") or []):
            volume = await real_daemon.volumes.get(volume_name)
            await volume.delete()
        await network.delete()
        await runner.cleanup()
