"""The tools that only look, driven through a real MCP client."""

from __future__ import annotations

import pytest
from aiohttp_tiny_mcp import Client, ClientError
from aiohttp_tiny_mcp.protocol.selection import AdapterSet

pytestmark = pytest.mark.asyncio

MODERN = AdapterSet.default().by_version["2026-07-28"]


async def test_the_server_says_what_it_offers(url):
    async with Client(url, MODERN) as client:
        result = await client.initialize()
        tools = {tool.name for tool in await client.list_tools()}
        prompts = {prompt.name for prompt in await client.list_prompts()}
        resources = {resource.uri for resource in await client.list_resources()}

    assert "Docker" in result["instructions"]
    assert tools == {
        "containers",
        "container",
        "logs",
        "read",
        "images",
        "networks",
        "volumes",
        "events",
        "stats",
        "info",
        "create",
        "start",
        "stop",
        "restart",
        "wait",
        "write",
        "exec",
        "remove",
        "remove_image",
        "prune",
        "pull",
    }
    assert prompts == {"diagnose", "reclaim"}
    assert resources == {"docker://info", "docker://containers", "docker://images"}


async def test_every_tool_says_what_it_is_for(url):
    async with Client(url, MODERN) as client:
        await client.initialize()
        tools = await client.list_tools()

    for tool in tools:
        assert tool.description, tool.name
        assert len(tool.description) > 40, f"{tool.name}: too terse to choose by"


async def test_anything_destructive_says_so(url):
    async with Client(url, MODERN) as client:
        await client.initialize()
        tools = {tool.name: tool for tool in await client.list_tools()}

    assert tools["remove"].annotations["destructiveHint"] is True
    assert tools["remove_image"].annotations["destructiveHint"] is True
    assert tools["prune"].annotations["destructiveHint"] is True
    assert tools["exec"].annotations["destructiveHint"] is True
    assert tools["write"].annotations["destructiveHint"] is True
    assert tools["containers"].annotations["readOnlyHint"] is True
    assert tools["read"].annotations["readOnlyHint"] is True


async def test_listing_containers_shows_the_running_ones(url):
    async with Client(url, MODERN) as client:
        await client.initialize()
        result = await client.call_tool("containers", {})

    listed = result.structured_content
    assert [item["name"] for item in listed["containers"]] == ["cache"]
    assert listed["containers"][0]["ports"] == ["0.0.0.0:6379->6379/tcp"]


async def test_listing_everything_includes_the_stopped_ones(url):
    async with Client(url, MODERN) as client:
        await client.initialize()
        result = await client.call_tool("containers", {"all": True})

    assert {item["name"] for item in result.structured_content["containers"]} == {
        "cache",
        "worker",
    }


async def test_containers_can_be_filtered_by_name(url):
    async with Client(url, MODERN) as client:
        await client.initialize()
        result = await client.call_tool("containers", {"all": True, "name": "work"})

    assert [item["name"] for item in result.structured_content["containers"]] == ["worker"]


async def test_a_container_is_found_by_name_or_by_id_prefix(url):
    async with Client(url, MODERN) as client:
        await client.initialize()
        by_name = await client.call_tool("container", {"container": "cache"})
        by_prefix = await client.call_tool("container", {"container": "111111"})

    assert by_name.structured_content["id"] == by_prefix.structured_content["id"]


async def test_environment_values_are_never_reported(url):
    """Environment values may contain secrets; expose names only."""
    async with Client(url, MODERN) as client:
        await client.initialize()
        result = await client.call_tool("container", {"container": "cache"})

    environment = result.structured_content["environment"]
    assert environment == ["PATH", "REDIS_PASSWORD"]
    assert "hunter2" not in str(result.structured_content)


async def test_an_unknown_container_is_said_plainly(url):
    async with Client(url, MODERN) as client:
        await client.initialize()
        result = await client.call_tool("container", {"container": "nothing-like-this"})

    assert result.is_error is True
    assert "no container matches" in result.content[0].text


async def test_logs_read_the_end_not_the_beginning(url):
    async with Client(url, MODERN) as client:
        await client.initialize()
        result = await client.call_tool("logs", {"container": "worker", "lines": 1})

    assert result.structured_content["lines"] == [
        "2026-09-13T10:00:02.000000000Z err ValueError: no such queue"
    ]
    assert result.structured_content["cursor"] == "2026-09-13T10:00:02.000000000Z"


async def test_images_are_listed_with_their_sizes(url):
    async with Client(url, MODERN) as client:
        await client.initialize()
        result = await client.call_tool("images", {})

    listed = {item["tags"][0]: item["size_mb"] for item in result.structured_content["images"]}
    assert listed == {"redis:7": 130.0, "postgres:16": 420.0}


async def test_images_can_be_filtered(url):
    async with Client(url, MODERN) as client:
        await client.initialize()
        result = await client.call_tool("images", {"reference": "postgres*"})

    assert result.structured_content["total"] == 1


async def test_info_reports_the_daemon(url):
    async with Client(url, MODERN) as client:
        await client.initialize()
        result = await client.call_tool("info", {})

    reported = result.structured_content
    assert reported["version"] == "27.1.0"
    assert reported["containers_running"] == 1
    assert reported["containers_total"] == 2
    assert reported["images"] == 2


async def test_resources_read_the_same_things(url):
    async with Client(url, MODERN) as client:
        await client.initialize()
        listed = await client.read_resource("docker://containers")
        one = await client.read_resource("docker://containers/cache")
        written = await client.read_resource("docker://containers/worker/logs")

    assert "cache" in listed.contents[0].text
    assert "healthy" in one.contents[0].text
    assert "ValueError" in written.contents[0].text


async def test_a_resource_that_matches_nothing_is_refused(url):
    async with Client(url, MODERN) as client:
        await client.initialize()
        with pytest.raises(ClientError):
            await client.read_resource("docker://nothing/here")


async def test_names_are_suggested_rather_than_typed_blind(url):
    async with Client(url, MODERN) as client:
        await client.initialize()
        containers = await client.complete(
            {"type": "ref/prompt", "name": "diagnose"},
            {"name": "container", "value": "c"},
        )
        images = await client.complete(
            {"type": "ref/prompt", "name": "diagnose"},
            {"name": "image", "value": "post"},
        )

    assert containers.completion.values == ["cache"]
    assert images.completion.values == ["postgres:16"]


async def test_the_prompts_say_how_to_go_about_it(url):
    async with Client(url, MODERN) as client:
        await client.initialize()
        diagnose = await client.get_prompt("diagnose", {"container": "worker"})
        reclaim = await client.get_prompt("reclaim", {})

    assert "worker" in diagnose.messages[0].content.text
    assert "logs" in diagnose.messages[0].content.text
    assert "Do not remove" in reclaim.messages[0].content.text
