# Many tenants on one server

A remote MCP server may answer for hundreds of unrelated callers. Every key
this package writes is composed with a namespace, so isolation holds by
construction rather than by remembering to check.

## Setting it

One contextvar, set by one middleware. Where the name comes from is yours.

<!-- name: test_namespaces -->
```python
from aiohttp import web

from aiohttp_tiny_mcp.namespaces import namespace


@web.middleware
async def tenant(request: web.Request, handler):
    namespace.set(request.headers.get("X-Tenant"))
    return await handler(request)
```

Mount it ahead of the endpoint:

<!-- name: test_namespaces -->
```python
from aiohttp_tiny_mcp import Endpoint, MemoryHub, MemorySessionStore, Registry

registry = Registry("service", "1.0", hub=MemoryHub(), session_store=MemorySessionStore())
app = web.Application(middlewares=[tenant])
Endpoint(registry).setup(app, "/mcp")
```

Set nothing and every caller shares one namespace, which is what a
single-tenant server wants.

<!-- name: test_namespaces -->
```python
from aiohttp_tiny_mcp.namespaces import scoped

namespace.set(None)
assert scoped("abc") == "abc"

namespace.set("acme")
assert scoped("abc") == "acme:abc"

# Percent-encoded, so a name containing the separator cannot reach into
# another namespace.
namespace.set("acme:evil")
assert scoped("abc") == "acme%3Aevil:abc"
namespace.set(None)
```

## What it covers

Everything: session ids, request-state ids, hub topics. The namespace is part
of the key, not something checked against it, so there is no path that forgets
to compare.

<!-- name: async test_namespace_isolation; fixtures: serve -->
```python
import pytest
from aiohttp import web
from pydantic import BaseModel

from aiohttp_tiny_mcp import (
    Client,
    Endpoint,
    Exchange,
    MemoryHub,
    MemorySessionStore,
    Registry,
)
from aiohttp_tiny_mcp.namespaces import namespace
from aiohttp_tiny_mcp.protocol.selection import AdapterSet


@web.middleware
async def tenant(request, handler):
    namespace.set(request.headers.get("X-Tenant"))
    return await handler(request)


registry = Registry("service", "1.0", hub=MemoryHub(), session_store=MemorySessionStore())


class Nothing(BaseModel):
    pass


class Handle(BaseModel):
    handle: str


@registry.tool
async def open_box(args: Nothing, ex: Exchange) -> str:
    """Mint a session and name it."""
    session = await ex.sessions.open()
    await session.set("secret", "kept")
    return session.id


@registry.tool
async def read_box(args: Handle, ex: Exchange) -> str:
    """Read a session by handle, if it is reachable from here."""
    session = await ex.sessions.use(args.handle)
    return "unreachable" if session is None else session.get("secret")


app = web.Application(middlewares=[tenant])
Endpoint(registry).setup(app, "/mcp")

runner = web.AppRunner(app)
await runner.setup()
site = web.TCPSite(runner, "127.0.0.1", 0)
await site.start()
host, port = runner.addresses[0]
url = f"http://{host}:{port}/mcp"
adapter = AdapterSet.default().by_version["2026-07-28"]

try:
    import aiohttp

    async with aiohttp.ClientSession(headers={"X-Tenant": "first"}) as http:
        async with Client(url, adapter, session=http) as client:
            await client.initialize()
            handle = (await client.call_tool("open_box", {})).content[0].text
            mine = await client.call_tool("read_box", {"handle": handle})
            assert mine.content[0].text == "kept"

    # The very same handle, from another tenant.
    async with aiohttp.ClientSession(headers={"X-Tenant": "second"}) as http:
        async with Client(url, adapter, session=http) as client:
            await client.initialize()
            theirs = await client.call_tool("read_box", {"handle": handle})
            assert theirs.content[0].text == "unreachable"
finally:
    await runner.cleanup()
```

A handle that leaks out of one tenant addresses nothing in another, even
quoted exactly.

## Where the name should come from

Whatever your deployment already trusts. An authenticated subject, an API key's
owner, a tenant claim in a token.

For bearer tokens verified by this library or by aiohttp middleware, see
[Authentication](../guide/auth.md).

Not the client's own say-so unless you verify it, and not `request.remote` on a
server behind a proxy -- every caller would then share the proxy's address. If
you split by address, read the forwarded header your own gateway sets and fall
back to the peer only where there is no gateway.

## What it does not do

It is isolation of *keys*, not authorization. It stops one tenant reaching
another's sessions, states and events. It does not decide who may call which
tool -- that is your middleware's job, and `ex.request` is where a handler
reads what it decided.
