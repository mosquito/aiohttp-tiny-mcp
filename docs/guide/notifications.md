# Notifications

Three things a server sends that are not answers: progress while a call runs,
log messages, and changes to what it offers. Each is written once and delivered
by whatever mechanism the client's revision has.

## Progress

A tool declared with `streaming=True` gets a response stream, and can report on
it before returning.

<!-- name: async test_progress; fixtures: serve -->
```python
from pydantic import BaseModel

from aiohttp_tiny_mcp import Client, Exchange, Registry
from aiohttp_tiny_mcp.protocol.selection import AdapterSet

registry = Registry("watcher", "1.0")


class Work(BaseModel):
    steps: int


@registry.tool(streaming=True)
async def crunch(args: Work, ex: Exchange) -> str:
    """Report progress, then finish."""
    for step in range(args.steps):
        await ex.progress(step, args.steps, message=f"step {step}")
    return f"did {args.steps}"


seen = []


async def note(frame):
    if frame.get("method") == "notifications/progress":
        seen.append(frame["params"]["progress"])


url = await serve(registry)
async with Client(url, AdapterSet.default().adapters[0], on_notification=note) as client:
    await client.initialize()
    result = await client.call_tool("crunch", {"steps": 3})

assert seen == [0, 1, 2]
assert result.content[0].text == "did 3"
```

A tool without `streaming=True` answers in one piece and has nowhere to report,
so `ex.progress` is silently ignored there. That is deliberate: a handler may be
called either way and should not have to check.

## Logging

`ex.log` takes one of the eight RFC 5424 severities.

<!-- name: async test_logging; fixtures: serve -->
```python
from pydantic import BaseModel

from aiohttp_tiny_mcp import Client, Exchange, Registry
from aiohttp_tiny_mcp.protocol.selection import AdapterSet

registry = Registry("watcher", "1.0")


class Nothing(BaseModel):
    pass


@registry.tool(streaming=True)
async def noisy(args: Nothing, ex: Exchange) -> str:
    """Report at three severities."""
    await ex.log("debug", "looking")
    await ex.log("warning", "odd", logger="noisy")
    await ex.log("error", "bad")
    return "done"


def collect(into):
    async def note(frame):
        if frame.get("method") == "notifications/message":
            into.append(frame["params"]["level"])

    return note


url = await serve(registry)
heard = []
async with Client(
    url, AdapterSet.default().adapters[0], log_level="warning", on_notification=collect(heard)
) as client:
    await client.initialize()
    await client.call_tool("noisy", {})

assert heard == ["warning", "error"]
```

A client receives the severity it asked for and everything above it. A client
that asked for nothing receives nothing:

<!-- name: async test_logging -->
```python
silent = []
async with Client(url, AdapterSet.default().adapters[0], on_notification=collect(silent)) as client:
    await client.initialize()
    await client.call_tool("noisy", {})

assert silent == []
```

`2026-07-28` states the level on every request. The older revisions state it
once with `logging/setLevel` and the server remembers it in the session. Either
way the handler sees one value, and `client.set_log_level` changes it the same
way on both.

## Changes to what the server offers

An application publishes a change once, into the hub, without knowing who is
reading.

<!-- name: async test_changes; fixtures: serve -->
```python
import asyncio

from pydantic import BaseModel

from aiohttp_tiny_mcp import Client, Registry
from aiohttp_tiny_mcp.hub import NOTIFICATIONS, topic
from aiohttp_tiny_mcp.protocol.selection import AdapterSet

registry = Registry("watcher", "1.0")


class Nothing(BaseModel):
    pass


@registry.resource("config://app", mime_type="application/json")
async def config(args: Nothing) -> dict:
    """Application configuration."""
    return {"debug": False}


async def announce():
    await registry.hub.publish(
        topic(NOTIFICATIONS),
        {
            "jsonrpc": "2.0",
            "method": "notifications/resources/updated",
            "params": {"uri": "config://app"},
        },
    )
```

A client subscribes with one call, whatever its revision.

<!-- name: async test_changes -->
```python
url = await serve(registry)

for adapter in AdapterSet.default().adapters:
    async with Client(url, adapter) as client:
        await client.initialize()
        stream = client.listen(resources=["config://app"], resources_changed=True)

        async def first():
            async for change in stream:
                return change

        heard = asyncio.ensure_future(first())
        # Published until it lands: a change sent before the subscription
        # exists is correctly not delivered, and this is a test rather than a
        # server that changes on its own.
        while not heard.done():
            await announce()
            await asyncio.sleep(0.02)

        change = await heard
        await stream.aclose()

    assert change["params"]["uri"] == "config://app", adapter.version
```

`2026-07-28` carries this on the stream of one long-lived `subscriptions/listen`
request, which also names exactly what it wants. The older revisions call
`resources/subscribe` once per resource and read a stream they open separately
with `GET`; there a client cannot filter list changes, so the capability it was
told about at the handshake is the whole of its choice.

The topics are:

| Published method | Reaches a client that asked for |
| --- | --- |
| `notifications/resources/updated` | that resource, by URI |
| `notifications/resources/list_changed` | `resources_changed` |
| `notifications/tools/list_changed` | `tools_changed` |
| `notifications/prompts/list_changed` | `prompts_changed` |
| A method an extension declared, sent with `registry.broadcast` | `methods=[...]` on `2026-07-28`, by name or `MethodFilter(method, topics)`; every declared one on older revisions |

Anything else published to the topic is ignored, so an application may use the
same hub for its own traffic.

## What a subscription is not

It does not survive a disconnect. A client that reconnects subscribes again,
and the server keeps nothing waiting for it. On the older revisions the *set*
of subscribed resources does live in the session, so a reconnecting client with
the same session id resumes without re-subscribing -- but the stream itself is
always new.
