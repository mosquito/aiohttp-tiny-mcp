# Sessions and state

Use a session when several calls need shared values, such as a user's current
selection or the progress of a workflow. Use request state when a handler needs
to preserve a computed value while waiting for the answer to one of its
questions. Both are stored through the `SessionStore` configured on the registry.

In a remote deployment, the next call can reach another process or machine.
The receiving worker reads the record from the shared store using the identifier
carried by the client. Writes check the record version and retry from fresh data
when another worker has updated it. See [Stores and hubs](../deployment/stores.md)
for the backend contract.

| State | Addressed by | Lifetime and owner |
| --- | --- | --- |
| Protocol session and application session values | HTTP session ID where supported, or an explicit application handle | Shared across calls until expiry or deletion |
| Explicit question state | Request-state identifier returned with the question | Managed by the library for the follow-up call |
| Handler locals and resolved Python dependencies | The current `Exchange` | Local to one invocation; not saved in the store |

The application chooses what values to put in a session or pass as question
state. Ordinary business records can stay in your application's database; they
do not need to be copied into an MCP session.

## Values that outlive a call

`ex.session` is the session this request reaches, where the revision has one.

<!-- name: async test_session; fixtures: serve -->
```python
from pydantic import BaseModel

from aiohttp_tiny_mcp import Client, Exchange, Registry
from aiohttp_tiny_mcp.protocol.selection import AdapterSet

registry = Registry("notes", "1.0")


class Seen(BaseModel):
    what: str


class Nothing(BaseModel):
    pass


@registry.tool
async def remember(args: Seen, ex: Exchange) -> str:
    """Add to what this caller has seen."""
    session = await reach(ex)
    seen = [*session.get("seen", []), args.what]
    await session.set("seen", seen)
    return f"{len(seen)} so far"
```

`reach` is written below, because how a session is addressed is the one thing
that genuinely differs.

## Addressing a session

A revision with a handshake has one already: the server issues `Mcp-Session-Id`
at `initialize`, the client returns it on every later request, and `ex.session`
is that session.

For `2026-07-28`, this library uses explicit application handles rather than
protocol sessions. The server creates a handle, returns it in a result, and
the caller sends it back as an ordinary argument on later calls.

`ex.sessions` reaches the same `Session` object either way:

<!-- name: async test_session -->
```python
async def reach(ex: Exchange, handle: str | None = None):
    """The session this call belongs to, on any revision."""
    if ex.session is not None:
        return ex.session  # a handshake opened one
    if handle is not None:
        found = await ex.sessions.use(handle)
        if found is not None:
            return found
    return await ex.sessions.open()  # mints a handle the caller must keep
```

A tool that serves `2026-07-28` therefore takes the handle as an argument and
returns it, exactly as the revision intends:

<!-- name: async test_session -->
```python
class Visit(BaseModel):
    what: str
    handle: str | None = None


@registry.tool
async def visit(args: Visit, ex: Exchange) -> dict:
    """Record a visit, and say where to send the next one."""
    session = await reach(ex, args.handle)
    seen = [*session.get("seen", []), args.what]
    await session.set("seen", seen)
    return {"handle": session.id, "seen": seen}


url = await serve(registry)
async with Client(url, AdapterSet.default().by_version["2026-07-28"]) as client:
    await client.initialize()
    first = await client.call_tool("visit", {"what": "a"})
    handle = first.structured_content["handle"]
    second = await client.call_tool("visit", {"what": "b", "handle": handle})

assert second.structured_content["seen"] == ["a", "b"]
```

On a revision with a handshake the same tool works without the argument, because
`ex.session` is already there:

<!-- name: async test_session -->
```python
async with Client(url, AdapterSet.default().by_version["2025-11-25"]) as client:
    await client.initialize()
    await client.call_tool("visit", {"what": "a"})
    second = await client.call_tool("visit", {"what": "b"})

assert second.structured_content["seen"] == ["a", "b"]
```

## Writing safely

Reads answer from the copy loaded with the request. Writes go through
compare-and-set: a worker that lost a race re-reads and applies the change
again rather than overwriting.

<!-- name: async test_session_cas; fixtures: serve -->
```python
import asyncio

from pydantic import BaseModel

from aiohttp_tiny_mcp import Client, Exchange, Registry
from aiohttp_tiny_mcp.protocol.selection import AdapterSet

registry = Registry("notes", "1.0")


class Bump(BaseModel):
    pass


@registry.tool
async def bump(args: Bump, ex: Exchange) -> int:
    """Increase a counter that several callers share."""
    session = ex.session or await ex.sessions.open()
    await session.update(lambda values: {**values, "n": values.get("n", 0) + 1})
    return session.get("n")


url = await serve(registry)
async with Client(url, AdapterSet.default().by_version["2025-11-25"]) as client:
    await client.initialize()
    await asyncio.gather(*(client.call_tool("bump", {}) for _ in range(10)))
    final = await client.call_tool("bump", {})

assert final.content[0].text == "11"
```

Pass a function, not a value. It runs again on each attempt against values
re-read from the store, which is the difference between retrying and silently
putting back what the first read saw.

## State between two calls

When a handler asks a question, whatever it leaves in `state` waits on the
server. The client carries only an identifier.

That identifier is unguessable, it is bound to the call it came from -- an id
issued for `purge cache` cannot finish `purge database` -- its key carries the
[namespace](../deployment/multitenancy.md), and it is dropped as soon as the
round trip ends, so it cannot be replayed.

<!-- name: async test_request_state; fixtures: serve -->
```python
import pytest
from pydantic import BaseModel

from aiohttp_tiny_mcp import (
    Client,
    ClientError,
    Exchange,
    NeedInput,
    Registry,
    elicit,
    elicit_accept,
)

registry = Registry("notes", "1.0")
from aiohttp_tiny_mcp.protocol.selection import AdapterSet


class Target(BaseModel):
    name: str


@registry.tool
async def purge(args: Target, ex: Exchange) -> str:
    """Confirm, then act on what was resolved before the question."""
    if ex.answered("confirm"):
        return f"purged {ex.state['resolved']}"
    raise NeedInput(
        {"confirm": elicit(f"Purge {args.name}?")},
        state={"resolved": args.name.upper()},
    )


url = await serve(registry)
adapter = AdapterSet.default().by_version["2026-07-28"]
answered = {"confirm": elicit_accept({})}

async with Client(url, adapter) as client:
    await client.initialize()
    asked = await client.call_tool("purge", {"name": "cache"})
    state = asked["requestState"]

    # An id from one call does not finish another.
    with pytest.raises(ClientError, match="requestState"):
        await client.call_tool(
            "purge", {"name": "database"}, input_responses=answered, request_state=state
        )

    done = await client.call_tool(
        "purge", {"name": "cache"}, input_responses=answered, request_state=state
    )
    assert done.content[0].text == "purged CACHE"

    # Spent once the round trip ended.
    with pytest.raises(ClientError, match="requestState"):
        await client.call_tool(
            "purge", {"name": "cache"}, input_responses=answered, request_state=state
        )
```

Because the state lives in the store and not in one worker's memory, the second
call may reach a different node than the first. Nothing routes a client back to
where it started, and nothing has to.

## Lifetimes

| | Default | Set with |
| --- | --- | --- |
| Session | 1 hour | `Registry(session_ttl_seconds=...)` |
| Request state | 10 minutes | `Registry(request_state_ttl_seconds=...)` |

Sessions and request state expire on separate schedules. Set the request-state
TTL long enough for the interaction you expect; a follow-up call with an expired
state identifier is rejected.
