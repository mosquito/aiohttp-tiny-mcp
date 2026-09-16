# Resources

A resource is something to read, named by a URI. Reading it changes nothing,
and it takes no arguments beyond what the URI itself carries. See
[Tools, resources, and prompts](../concepts.md) for why that matters: the host decides
what to put in front of the model, so a resource is context rather than an
action.

<!-- name: test_resources -->
```python
from pydantic import BaseModel

from aiohttp_tiny_mcp import MemoryHub, MemorySessionStore, Registry

registry = Registry("notebook", "1.0", hub=MemoryHub(), session_store=MemorySessionStore())


class Nothing(BaseModel):
    """No arguments. A fixed resource has none."""
```

## A fixed resource

<!-- name: test_resources -->
```python
@registry.resource("config://app", mime_type="application/json")
async def config(args: Nothing) -> dict:
    """Application configuration."""
    return {"debug": False, "region": "eu"}
```

The URI is whatever scheme suits you. `file://` and `https://` mean what they
usually mean; anything else is yours to define, and a scheme that names your
domain is the convention.

The docstring is the description, as with a tool. Here it is read by a person
choosing what to attach, more often than by a model.

## A template

A URI with variables describes a family of resources. The variables arrive as
fields of the argument model, so they are validated like any other input.

<!-- name: test_resources -->
```python
class Entry(BaseModel):
    id: int


@registry.resource("notebook://entries/{id}", name="entry")
async def entry(args: Entry) -> str:
    """One notebook entry by id."""
    return f"entry {args.id}"
```

A variable matches one path segment: `{id}` does not span a `/`.

Fixed resources appear in `resources/list`. Templates appear in
`resources/templates/list`, because a client cannot enumerate them -- it has to
be told the shape and fill it in.

<!-- name: async test_resources; fixtures: serve -->
```python
from aiohttp_tiny_mcp import Client
from aiohttp_tiny_mcp.protocol.selection import AdapterSet

url = await serve(registry)
async with Client(url, AdapterSet.default().by_version["2026-07-28"]) as client:
    await client.initialize()
    listed = await client.list_resources()
    read = await client.read_resource("notebook://entries/42")

assert {resource.uri for resource in listed} == {"config://app"}
assert read.contents[0].text == "entry 42"
```

## What a handler may return

<!-- name: test_resources -->
```python
@registry.resource("notebook://cover.png", mime_type="image/png")
async def cover(args: Nothing) -> bytes:
    """The cover image, as bytes."""
    return b"\x89PNG\r\n\x1a\n"
```

| Returned | Becomes |
| --- | --- |
| a string | text contents, `text/plain` unless `mime_type` says otherwise |
| `bytes` | base64 blob contents, `application/octet-stream` by default |
| a dict, list or model | JSON text, `application/json` by default |
| `TextResourceContents` or `BlobResourceContents` | itself, unchanged |

Set `mime_type` where the default is wrong. It is what tells a host whether to
render the thing, and how.

## Reading one that is not there

A URI that matches no resource and no template is a `-32002`, resource not
found. That is a protocol failure rather than a result, because -- unlike a
tool call -- there was nothing to reach.

<!-- name: async test_resources; fixtures: serve -->
```python
import pytest

from aiohttp_tiny_mcp import ClientError

async with Client(url, AdapterSet.default().by_version["2026-07-28"]) as client:
    await client.initialize()
    with pytest.raises(ClientError):
        await client.read_resource("notebook://nothing/here")
```

## Telling clients it changed

Publish once; whoever subscribed hears about it, whatever revision they speak.

<!-- name: test_resources -->
```python
from aiohttp_tiny_mcp.hub import NOTIFICATIONS, topic


async def announce(uri: str) -> None:
    """Say that a resource changed. Who is listening is not this code's
    concern -- see the notifications guide."""
    await registry.hub.publish(
        topic(NOTIFICATIONS),
        {
            "jsonrpc": "2.0",
            "method": "notifications/resources/updated",
            "params": {"uri": uri},
        },
    )
```

See [Notifications](notifications.md#changes-to-what-the-server-offers).

## Cache hints

`2026-07-28` lets a result say how long it may be cached and by whom. The
default is not to cache, which is the safe answer for anything that depends on
who is asking.

<!-- name: test_resources -->
```python
@registry.resource(
    "notebook://schema",
    mime_type="application/json",
    cache_ttl_ms=300_000,
    cache_scope="public",
)
async def schema(args: Nothing) -> dict:
    """The same for every caller, and it rarely changes."""
    return {"version": 3}
```

Say `public` only where the answer genuinely does not depend on the caller. On
the older revisions the hints are absent entirely and the fields are ignored.

## When a resource is the wrong shape

If reading it changes something, it is a tool. If the caller has to supply
anything that is not part of an identifier, it is a tool -- resources are
addressed, not called. And if the model rather than the host should decide when
to fetch it, it is a tool.
