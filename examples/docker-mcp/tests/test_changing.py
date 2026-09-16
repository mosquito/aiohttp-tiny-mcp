"""Mutating tools and confirmation delivery across protocol revisions."""

from __future__ import annotations

import pytest
from aiohttp_tiny_mcp import (
    Client,
    ClientError,
    elicit_accept,
    elicit_cancel,
    elicit_decline,
)
from aiohttp_tiny_mcp.protocol.selection import AdapterSet

pytestmark = pytest.mark.asyncio

EVERY_REVISION = AdapterSet.default().adapters
MODERN = AdapterSet.default().by_version["2026-07-28"]


def agreeing(seen: list[str] | None = None):
    async def answer(request):
        if seen is not None:
            seen.append(request["params"]["message"])
        return elicit_accept({"confirmed": True})

    return answer


async def test_a_container_is_started_and_stopped(url, fake):
    async with Client(url, MODERN) as client:
        await client.initialize()
        started = await client.call_tool("start", {"container": "worker"})
        stopped = await client.call_tool("stop", {"container": "cache", "seconds": 1})

    assert started.structured_content["state"] == "running"
    assert stopped.structured_content["state"] == "exited"
    assert fake.started == ["3333333333334444444444"]
    assert fake.stopped == ["1111111111112222222222"]


async def test_restarting_reports_the_state_it_ended_in(url):
    async with Client(url, MODERN) as client:
        await client.initialize()
        result = await client.call_tool("restart", {"container": "cache"})

    assert result.structured_content == {
        "container": "cache",
        "action": "restart",
        "state": "running",
        "exit_code": None,
        "health": "healthy",
        "note": None,
    }


@pytest.mark.parametrize("adapter", EVERY_REVISION, ids=lambda a: a.version)
async def test_removing_asks_first_on_every_revision(url, fake, adapter):
    """Exercise MRTR, pushed elicitation, and tool-argument confirmations."""
    asked: list[str] = []
    async with Client(url, adapter, on_ask=agreeing(asked)) as client:
        await client.initialize()
        result = await client.call_tool("remove", {"container": "worker"})

    assert len(asked) == 1
    assert "cannot be undone" in asked[0]
    assert result.content[0].text == "removed worker"
    assert fake.removed == ["3333333333334444444444"]


@pytest.mark.parametrize(
    "reply,expected",
    [(elicit_decline(), "decline"), (elicit_cancel(), "cancel")],
    ids=["declined", "cancelled"],
)
async def test_a_refusal_removes_nothing(url, fake, reply, expected):
    async def answer(request):
        return reply

    async with Client(url, MODERN, on_ask=answer) as client:
        await client.initialize()
        result = await client.call_tool("remove", {"container": "worker"})

    assert result.content[0].text == f"not removed: {expected}"
    assert fake.removed == []


async def test_saying_no_inside_an_accepted_form_removes_nothing(url, fake):
    """An accepted form with confirmed=false is still a refusal."""

    async def answer(request):
        return elicit_accept({"confirmed": False})

    async with Client(url, MODERN, on_ask=answer) as client:
        await client.initialize()
        result = await client.call_tool("remove", {"container": "worker"})

    assert "not removed" in result.content[0].text
    assert fake.removed == []


async def test_a_client_that_cannot_be_asked_removes_nothing(url, fake):
    """No on_ask means pushed questions cannot be answered."""
    legacy = AdapterSet.default().by_version["2025-11-25"]
    async with Client(url, legacy) as client:
        await client.initialize()
        with pytest.raises(ClientError, match="cannot ask"):
            await client.call_tool("remove", {"container": "worker"})

    assert fake.removed == []


async def test_a_reading_command_runs_without_interrupting_anybody(url, fake):
    asked: list[str] = []
    async with Client(url, MODERN, on_ask=agreeing(asked)) as client:
        await client.initialize()
        result = await client.call_tool("exec", {"container": "cache", "command": ["ls", "/"]})

    assert asked == []
    assert result.is_error is False, result.content[0].text
    reported = result.structured_content
    assert reported["exit_code"] == 0
    assert "boot" in reported["stdout"]
    assert [entry["Cmd"] for entry in fake.executed] == [["ls", "/"]]


async def test_a_command_that_could_change_something_asks_first(url, fake):
    asked: list[str] = []
    async with Client(url, MODERN, on_ask=agreeing(asked)) as client:
        await client.initialize()
        result = await client.call_tool(
            "exec", {"container": "cache", "command": ["sh", "-c", "echo hi"]}
        )

    assert "Run `sh -c echo hi` inside cache?" in asked[0]
    assert result.is_error is False, result.content[0].text


async def test_a_refused_command_does_not_run(url, fake):
    async def answer(request):
        return elicit_decline()

    async with Client(url, MODERN, on_ask=answer) as client:
        await client.initialize()
        result = await client.call_tool(
            "exec", {"container": "cache", "command": ["rm", "-rf", "/"]}
        )

    assert result.structured_content["exit_code"] == -1
    assert fake.executed == []


@pytest.mark.parametrize("adapter", EVERY_REVISION, ids=lambda a: a.version)
async def test_a_permission_is_remembered_on_every_revision(url, fake, adapter):
    """Grants must survive separate requests, including revisions without protocol sessions."""
    asked: list[str] = []

    async def answer(request):
        asked.append(request["params"]["message"])
        return elicit_accept({"confirmed": True, "remember": "any"})

    async with Client(url, adapter, on_ask=answer) as client:
        await client.initialize()
        await client.call_tool("remove_image", {"image": "postgres:16"})
        await client.call_tool("remove_image", {"image": "redis:7"})

    assert len(asked) == 1, "the second removal asked again"
    assert fake.removed_images == ["postgres:16", "redis:7"]


async def test_the_choice_is_offered_only_where_it_can_be_kept(url, fake):
    schemas: list[dict] = []

    async def answer(request):
        schemas.append(request["params"]["requestedSchema"])
        return elicit_accept({"confirmed": True})

    async with Client(url, MODERN, on_ask=answer) as client:
        await client.initialize()
        await client.call_tool("remove", {"container": "worker"})

    assert "remember" in schemas[0]["properties"]


async def test_a_file_is_read_and_written_through_the_server(url, fake):
    async with Client(url, MODERN, on_ask=agreeing()) as client:
        await client.initialize()
        before = await client.call_tool(
            "read", {"container": "worker", "path": "/app/settings.ini"}
        )
        await client.call_tool(
            "write",
            {
                "container": "worker",
                "path": "/app/settings.ini",
                "content": "[queue]\nname = jobs\n",
            },
        )
        after = await client.call_tool("read", {"container": "worker", "path": "/app/settings.ini"})

    assert "name = missing" in before.structured_content["text"]
    assert "name = jobs" in after.structured_content["text"]


async def test_creating_a_container_is_one_call(url, fake):
    async with Client(url, MODERN, on_ask=agreeing()) as client:
        await client.initialize()
        result = await client.call_tool(
            "create", {"image": "redis:7", "name": "fresh", "ports": ["8080:6379"]}
        )

    assert result.is_error is False, result.content[0].text
    assert result.structured_content["name"] == "fresh"
    assert result.structured_content["state"] == "running"
    assert fake.created[0]["name"] == "fresh"


async def test_pulling_reports_progress_as_it_goes(url, fake):
    progress = []
    messages = []

    async def note(frame):
        if frame.get("method") == "notifications/progress":
            progress.append(frame["params"]["progress"])
        if frame.get("method") == "notifications/message":
            messages.append(frame["params"]["data"])

    async with Client(url, MODERN, log_level="info", on_notification=note) as client:
        await client.initialize()
        result = await client.call_tool("pull", {"image": "redis:7-alpine"})

    assert fake.pulled == ["redis:7-alpine"]
    assert result.content[0].text == "pulled redis:7-alpine (3 layers)"
    assert progress, "a pull that says nothing looks like a hang"
    assert progress == sorted(progress), "progress must not go backwards"
    assert messages[0].startswith("pulling")
    assert messages[-1].startswith("pulled")
