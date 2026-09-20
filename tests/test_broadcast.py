"""Extension broadcasts: one `registry.broadcast` call reaches every listening client.

A broadcast goes through the hub's notification topic, so it is filtered like
a list change: a `subscriptions/listen` stream names the methods it wants, a
legacy stream gets every declared one.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

import aiohttp
import pytest
from aiohttp import web
from pydantic import BaseModel

from aiohttp_tiny_mcp import Client, Extension, MemoryHub, MethodFilter, Registry, SseEndpoint
from aiohttp_tiny_mcp.hub import NOTIFICATIONS, topic
from aiohttp_tiny_mcp.protocol.selection import AdapterSet
from aiohttp_tiny_mcp.sqlite import SqliteHub, SqliteSessionStore, SqliteStorage
from aiohttp_tiny_mcp.subscriptions import SUBSCRIPTION_ID
from aiohttp_tiny_mcp.testing import connect, serving

pytestmark = pytest.mark.timeout(20)

TASK = "notifications/backlog/task"
NOTE = "notifications/backlog/note"
MODERN = AdapterSet.default().by_version["2026-07-28"]
LEGACY = AdapterSet.default().by_version["2025-11-25"]
STREAMING = [a for a in AdapterSet.default().adapters if a.version != "2024-11-05"]


class Nothing(BaseModel):
    pass


class Watched(MemoryHub):
    """Counts the readers that took a starting position on the notification topic."""

    def __init__(self) -> None:
        super().__init__()
        self.readers = 0
        self.changed = asyncio.Event()

    async def position(self, where: str) -> str:
        found = await super().position(where)
        if where == topic(NOTIFICATIONS):
            self.readers += 1
            self.changed.set()
        return found

    async def readers_reach(self, count: int) -> None:
        while self.readers < count:
            self.changed.clear()
            await self.changed.wait()


def backlog(*notifications: str) -> Extension:
    extension = Extension("example.org/backlog", notifications=notifications or (TASK, NOTE))

    @extension.method("backlog/ping")
    async def ping(args: Nothing) -> dict:
        return {"ok": True}

    return extension


@pytest.fixture
def registry() -> Registry:
    reg = Registry("broadcasting", "1.0", hub=Watched(), hub_poll_seconds=0.2)
    reg.extension(backlog())

    @reg.tool
    async def noop(args: Nothing) -> str:
        """Makes the server advertise tool list changes."""
        return ""

    return reg


async def first(frames: AsyncIterator[dict], *, within: float = 5) -> dict:
    return await asyncio.wait_for(frames.__anext__(), within)


# --- declarations -----------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    ["backlog/task", "notifications/", "notifications//task", "notifications/a b", "x"],
)
def test_a_broadcast_name_must_be_a_notification(name):
    with pytest.raises(ValueError, match="invalid broadcast"):
        Extension("example.org/bad", notifications=[name])


@pytest.mark.parametrize(
    "name",
    [
        "notifications/tools/list_changed",
        "notifications/resources/updated",
        "notifications/message",
        "notifications/initialized",
        "notifications/cancelled",
        "notifications/subscriptions/acknowledged",
    ],
)
def test_the_protocols_own_notifications_cannot_be_declared(name):
    with pytest.raises(ValueError, match="protocol notification"):
        Extension("example.org/bad", notifications=[name])


def test_two_extensions_cannot_declare_the_same_broadcast():
    registry = Registry("dup", "1")
    registry.extension(Extension("example.org/one", notifications=[TASK]))
    with pytest.raises(ValueError, match="duplicate broadcast"):
        registry.extension(Extension("example.org/two", notifications=[TASK]))
    assert "example.org/two" not in registry.extensions


async def test_an_undeclared_method_is_refused_and_nothing_is_published(registry):
    where = topic(NOTIFICATIONS)
    before = await registry.hub.position(where)
    with pytest.raises(ValueError, match="no installed extension declares"):
        await registry.broadcast("notifications/backlog/other", {"id": 1})
    assert await registry.hub.position(where) == before


async def test_broadcast_publishes_a_notification_and_returns_its_id(registry):
    where = topic(NOTIFICATIONS)
    reader = await registry.hub.subscribe(where, wait=0.1)
    sent = await registry.broadcast(TASK, {"id": 12, "status": "done"}, meta={"who": "me"})
    [event] = await reader.poll()
    assert event.id == sent
    assert event.message == {
        "jsonrpc": "2.0",
        "method": TASK,
        "params": {"id": 12, "status": "done", "_meta": {"who": "me"}},
    }


# --- discovery --------------------------------------------------------------


async def test_the_capability_block_and_the_manifest_list_the_broadcasts(registry):
    async with connect(registry, adapter=MODERN) as modern:
        discovered = await modern.initialize()
        block = discovered["capabilities"]["extensions"]["example.org/backlog"]
        assert block["notifications"] == {NOTE: None, TASK: None}

    async with connect(registry, adapter=LEGACY) as legacy:
        found = await legacy.read_resource("mcp-extensions://example.org/backlog/manifest.json")
        manifest = json.loads(found.contents[0].text)
        assert manifest["notifications"] == {NOTE: None, TASK: None}


# --- delivery ---------------------------------------------------------------


@pytest.mark.parametrize("adapter", STREAMING, ids=[a.version for a in STREAMING])
async def test_two_listeners_each_get_one_broadcast(registry, adapter):
    hub = registry.hub
    async with serving(registry) as url:
        async with Client(url, adapter) as one, Client(url, adapter) as two:
            await one.initialize()
            await two.initialize()
            streams = [one.listen(methods=[TASK]), two.listen(methods=[TASK])]
            readings = [asyncio.ensure_future(first(stream)) for stream in streams]
            await hub.readers_reach(2)
            await registry.broadcast(TASK, {"id": 12, "status": "done"})
            frames = await asyncio.gather(*readings)
            for stream in streams:
                await stream.aclose()
    for frame in frames:
        assert frame["method"] == TASK, adapter.version
        assert frame["params"]["id"] == 12
        assert frame["params"]["status"] == "done"
    if adapter is MODERN:
        assert all(SUBSCRIPTION_ID in frame["params"]["_meta"] for frame in frames)


async def test_a_modern_listener_that_did_not_ask_hears_nothing(registry):
    hub = registry.hub
    async with serving(registry) as url:
        async with Client(url, MODERN) as asked, Client(url, MODERN) as silent:
            wanted = asked.listen(methods=[TASK])
            unwanted = silent.listen(tools_changed=True, methods=["notifications/backlog/nope"])
            readings = [asyncio.ensure_future(first(s)) for s in (wanted, unwanted)]
            await hub.readers_reach(2)
            while not (asked.accepted and silent.accepted):
                await asyncio.sleep(0.01)  # the acknowledgments are on their way
            assert silent.accepted == {"toolsListChanged": True}
            assert asked.accepted == {"methods": [TASK]}
            await registry.broadcast(TASK, {"id": 1})
            await hub.publish(
                topic(NOTIFICATIONS),
                {"jsonrpc": "2.0", "method": "notifications/tools/list_changed", "params": {}},
            )
            heard, other = await asyncio.gather(*readings)
            await wanted.aclose()
            await unwanted.aclose()
    assert heard["method"] == TASK
    assert other["method"] == "notifications/tools/list_changed"


async def sse_events(response: aiohttp.ClientResponse) -> AsyncIterator[tuple[str, dict]]:
    """(event name, JSON data) pairs from a 2024-11-05 stream."""
    event = ""
    async for raw in response.content:
        line = raw.decode().strip()
        if line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:"):
            data = line[5:].strip()
            yield event, json.loads(data) if event == "message" else {"endpoint": data}


async def test_the_oldest_transport_relays_broadcasts_to_two_streams(registry):
    """2024-11-05 has its own stream per client; broadcasts reach it too."""
    hub = registry.hub
    app = web.Application()
    SseEndpoint(registry).setup(app)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    host, port = runner.addresses[0]
    base = f"http://{host}:{port}"
    try:
        async with aiohttp.ClientSession() as http:
            streams = [
                await http.get(f"{base}/sse", headers={"Accept": "text/event-stream"})
                for _ in range(2)
            ]
            readers = [sse_events(stream) for stream in streams]
            for reader in readers:
                event, _ = await first(reader)
                assert event == "endpoint"
            await hub.readers_reach(2)
            await registry.broadcast(TASK, {"id": 7})
            frames = [await first(reader) for reader in readers]
            for stream in streams:
                stream.close()
    finally:
        await runner.cleanup()
    for event, frame in frames:
        assert event == "message"
        assert frame["method"] == TASK
        assert frame["params"] == {"id": 7}


async def test_stdio_listeners_get_broadcasts_too(registry):
    hub = registry.hub
    async with connect(registry, adapter=MODERN) as client:
        stream = client.listen(methods=[TASK, NOTE])
        reading = asyncio.ensure_future(first(stream))
        await hub.readers_reach(1)
        await registry.broadcast(NOTE, {"id": 5, "title": "renamed"})
        frame = await reading
        await stream.aclose()
    assert frame["method"] == NOTE
    assert frame["params"]["title"] == "renamed"


# --- multicast: topics within a method ----------------------------------------

PROJECT = "notifications/backlog/project"


def multicasting() -> Registry:
    reg = Registry("multicasting", "1.0", hub=Watched(), hub_poll_seconds=0.2)
    reg.extension(Extension("example.org/projects", notifications={PROJECT: "project", TASK: None}))

    @reg.tool
    async def noop(args: Nothing) -> str:
        """Makes the server advertise tool list changes."""
        return ""

    return reg


@pytest.mark.parametrize("field", ["", "_meta", 3])
def test_a_topic_field_must_be_a_usable_params_key(field):
    with pytest.raises(ValueError, match="invalid topic field"):
        Extension("example.org/bad", notifications={PROJECT: field})


async def test_a_multicast_needs_its_topic_and_publishes_nothing_without_it():
    registry = multicasting()
    where = topic(NOTIFICATIONS)
    before = await registry.hub.position(where)
    with pytest.raises(ValueError, match="multicast"):
        await registry.broadcast(PROJECT, {"id": 1})
    with pytest.raises(ValueError, match="multicast"):
        await registry.broadcast(PROJECT, {"id": 1, "project": 7})
    assert await registry.hub.position(where) == before
    await registry.broadcast(PROJECT, {"id": 1, "project": "a"})


async def test_the_declaration_shows_the_topic_field():
    registry = multicasting()
    async with connect(registry, adapter=MODERN) as modern:
        block = (await modern.initialize())["capabilities"]["extensions"]["example.org/projects"]
        assert block["notifications"] == {PROJECT: "project", TASK: None}
    async with connect(registry, adapter=LEGACY) as legacy:
        found = await legacy.read_resource("mcp-extensions://example.org/projects/manifest.json")
        assert json.loads(found.contents[0].text)["notifications"] == {
            PROJECT: "project",
            TASK: None,
        }


async def test_listeners_get_the_topics_they_named_and_a_bare_name_gets_all():
    registry = multicasting()
    hub = registry.hub
    heard: dict[str, list[str]] = {"a": [], "b": [], "all": []}

    async def collect(client: Client, into: list[str], methods: list, count: int) -> None:
        stream = client.listen(methods=methods)
        async for frame in stream:
            into.append(frame["params"]["id"])
            if len(into) == count:
                break
        await stream.aclose()

    async with serving(registry) as url:
        async with (
            Client(url, MODERN) as on_a,
            Client(url, MODERN) as on_b,
            Client(url, MODERN) as on_all,
        ):
            readers = [
                asyncio.ensure_future(
                    collect(on_a, heard["a"], [MethodFilter(method=PROJECT, topics=["a"])], 2)
                ),
                asyncio.ensure_future(
                    collect(on_b, heard["b"], [{"method": PROJECT, "topics": ["b"]}], 1)
                ),
                asyncio.ensure_future(collect(on_all, heard["all"], [PROJECT], 3)),
            ]
            await hub.readers_reach(3)
            while not (on_a.accepted and on_b.accepted and on_all.accepted):
                await asyncio.sleep(0.01)
            assert on_a.accepted == {"methods": [{"method": PROJECT, "topics": ["a"]}]}
            assert on_all.accepted == {"methods": [PROJECT]}
            await registry.broadcast(PROJECT, {"id": "a1", "project": "a"})
            await registry.broadcast(PROJECT, {"id": "b1", "project": "b"})
            await registry.broadcast(PROJECT, {"id": "a2", "project": "a"})
            await asyncio.wait_for(asyncio.gather(*readers), 5)
    assert heard == {"a": ["a1", "a2"], "b": ["b1"], "all": ["a1", "b1", "a2"]}


async def test_the_acknowledgment_keeps_what_the_server_can_filter_on():
    """Filters on one method merge; a bare name outranks them; a filter on a
    plain broadcast loses its topics; an undeclared method is dropped."""
    registry = multicasting()

    async def acknowledged(client: Client, methods: list) -> dict:
        client.accepted = {}
        stream = client.listen(methods=methods)
        reading = asyncio.ensure_future(first(stream))
        while not client.accepted:
            await asyncio.sleep(0.01)
        reading.cancel()
        await asyncio.gather(reading, return_exceptions=True)  # let the generator stop
        await stream.aclose()
        return dict(client.accepted)

    async with connect(registry, adapter=MODERN) as client:
        merged_filters = await acknowledged(
            client,
            [
                {"method": PROJECT, "topics": ["a"]},
                {"method": PROJECT, "topics": ["b", "a"]},
                {"method": TASK, "topics": ["x"]},
                {"method": "notifications/backlog/nope", "topics": ["x"]},
            ],
        )
        outranked = await acknowledged(client, [{"method": PROJECT, "topics": ["a"]}, PROJECT])
    assert merged_filters == {"methods": [{"method": PROJECT, "topics": ["a", "b"]}, TASK]}
    assert outranked == {"methods": [PROJECT]}


async def test_legacy_streams_get_every_topic():
    registry = multicasting()
    hub = registry.hub
    async with serving(registry) as url:
        async with Client(url, LEGACY) as client:
            await client.initialize()
            stream = client.stream_notifications()
            reading = asyncio.ensure_future(first(stream))
            await hub.readers_reach(1)
            await registry.broadcast(PROJECT, {"id": 1, "project": "elsewhere"})
            frame = await reading
            await stream.aclose()
    assert frame["params"] == {"id": 1, "project": "elsewhere"}


# --- resumption and other nodes ---------------------------------------------


async def test_a_resumed_legacy_stream_replays_a_missed_broadcast(registry):
    hub = registry.hub
    async with serving(registry) as url:
        async with Client(url, LEGACY) as client:
            await client.initialize()
            stream = client.stream_notifications()
            reading = asyncio.ensure_future(first(stream))
            await hub.readers_reach(1)
            await registry.broadcast(TASK, {"n": 1})
            assert (await reading)["params"] == {"n": 1}
            await stream.aclose()
            held = client.last_event_id
            assert held is not None

            await registry.broadcast(TASK, {"n": 2})
            await registry.broadcast(TASK, {"n": 3})

            again = client.stream_notifications(last_event_id=held)
            replayed = [await first(again), await first(again)]
            await again.aclose()
    assert [frame["params"]["n"] for frame in replayed] == [2, 3]


async def test_a_broadcast_on_one_node_reaches_a_listener_on_another(tmp_path):
    storage = SqliteStorage(str(tmp_path / "shared.sqlite"))

    def node(name: str) -> Registry:
        reg = Registry(
            name,
            "1.0",
            hub=SqliteHub(storage, look_again=0.02),
            session_store=SqliteSessionStore(storage),
            hub_poll_seconds=0.2,
        )
        reg.extension(backlog())
        return reg

    serving_node, sending_node = node("a"), node("b")
    try:
        async with serving(serving_node) as url:
            async with Client(url, MODERN) as client:
                stream = client.listen(methods=[TASK])
                reading = asyncio.ensure_future(first(stream))
                while not client.accepted:
                    await asyncio.sleep(0.01)
                await sending_node.broadcast(TASK, {"id": 3, "status": "done"})
                frame = await reading
                await stream.aclose()
    finally:
        await storage.close()
    assert frame["method"] == TASK
    assert frame["params"]["id"] == 3
