# Many tenants on one server

A remote MCP server can serve unrelated callers. Authentication policies select
a storage namespace from each verified principal to separate their MCP state.

## Setting it

Authentication policies return a `Principal`; its namespace selects the
session and Hub keys used by the request. See the
[authentication guide](../guide/auth.md#principal-and-storage-namespaces)
for policy configuration and identity mapping.

At the storage level, `namespace` is a context variable. An unset namespace
leaves keys unprefixed:

<!-- name: test_namespaces -->
```python
from aiohttp_tiny_mcp.storage.namespaces import namespace, scoped

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

The library prefixes MCP session keys, request-state keys, and Hub topics with
the selected namespace. Accounts in the same namespace share notifications;
session ownership is checked separately. Application data needs its own access
checks. Direct store calls must use scoped keys when namespace isolation is needed.

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
    StaticBasicAuth,
)
from aiohttp_tiny_mcp.protocol.selection import AdapterSet


# Each verified account gets its own storage namespace.
registry = Registry(
    "service",
    "1.0",
    auth=StaticBasicAuth(
        ("first", "example-password"),
        ("second", "example-password"),
    ),
    hub=MemoryHub(),
    session_store=MemorySessionStore(),
)


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


app = web.Application()
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
    from aiohttp import encode_basic_auth

    headers = {"Authorization": encode_basic_auth("first", "example-password")}
    async with aiohttp.ClientSession(headers=headers) as http:
        async with Client(url, adapter, session=http) as client:
            await client.initialize()
            handle = (await client.call_tool("open_box", {})).content[0].text
            mine = await client.call_tool("read_box", {"handle": handle})
            assert mine.content[0].text == "kept"

    # The very same handle, from another tenant.
    headers = {"Authorization": encode_basic_auth("second", "example-password")}
    async with aiohttp.ClientSession(headers=headers) as http:
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

Use verified identity or tenant information. The
[authentication guide](../guide/auth.md#principal-and-storage-namespaces)
explains storage isolation through `Principal.namespace` and session ownership
through `Principal.identity`.
Do not derive tenant identity from an unverified client header.

## What it does not do

Namespaces separate storage keys. They do not grant access to tools or to
application records. See [handler permissions](../guide/auth.md#handler-permissions)
for scope checks and application-level authorization.
