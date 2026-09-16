# Dependencies

Use dependency injection to give handlers your application's database client,
HTTP client, or other services. Annotate a parameter after the argument model
with the dependency type and register its provider before registering the
handler. This parameter is supplied by the server and is not part of the tool's
input schema.

Providers and their Python objects belong to the worker process. In a remote
deployment, each worker may have its own database connection pool pointing to
the same shared database. Registering a Python object does not synchronize its
in-memory state with other workers. `Exchange` is provided automatically;
application dependencies need one of the registrations below.

<!-- name: test_dependencies; fixtures: __name__ -->
```python
from pydantic import BaseModel

from aiohttp_tiny_mcp import MemoryHub, MemorySessionStore, Registry

registry = Registry("service", "1.0", hub=MemoryHub(), session_store=MemorySessionStore())


class Nothing(BaseModel):
    pass
```

A source is one of four things.

## An object you already have

The common case: something built once at start-up and the same on every
request.

<!-- name: async test_dependencies; fixtures: serve -->
```python
from aiohttp_tiny_mcp import Client
from aiohttp_tiny_mcp.protocol.selection import AdapterSet


class Database:
    async def count(self) -> int:
        return 41


registry.provide_instance(Database())


class Rows(BaseModel):
    total: int


@registry.tool
async def rows(args: Nothing, db: Database) -> Rows:
    """Ask the database, which was supplied rather than imported."""
    return Rows(total=await db.count() + 1)


url = await serve(registry)
async with Client(url, AdapterSet.default().by_version["2025-06-18"]) as client:
    await client.initialize()
    result = await client.call_tool("rows", {})

assert result.structured_content == {"total": 42}
```

The type comes from the object, so it is not written twice and cannot
disagree with itself. Pass a second argument where a handler asks for
something the object is not exactly -- a base class, or a protocol it
satisfies:

<!-- name: test_dependencies -->
```python
class Postgres(Database):
    pass


registry.provide_instance(Postgres(), Database)
```

## A factory

An async callable taking the exchange, called once per request. Use it where
the object depends on the request, or has to be built anew each time.

<!-- name: test_dependencies -->
```python
class Clock:
    pass


async def open_clock(ex) -> Clock:
    return Clock()


registry.provide(Clock, open_clock)
```

Called once per request, not once per handler: two parameters of the same type
in one call get the same object.

## An application key

For anything the aiohttp application already keeps.

<!-- name: test_dependencies -->
```python
from aiohttp import web

CACHE: web.AppKey[dict] = web.AppKey("cache", dict)
registry.provide(dict, CACHE)
```

The value is read from `request.app[CACHE]` when a handler asks for a `dict`.
This is the right form for anything set up in the application's own startup --
a connection pool, a client session, configuration.

## An async generator

For anything that must be released when the request ends: a transaction, a
lock, a borrowed connection.

<!-- name: async test_dependencies; fixtures: serve -->
```python
released = []


class Transaction:
    pass


async def transaction(ex):
    handle = Transaction()
    try:
        yield handle
    finally:
        released.append(handle)


registry.provide(Transaction, transaction)


@registry.tool
async def write(args: Nothing, tx: Transaction) -> str:
    """The transaction is closed when this returns, however it returns."""
    return "written"


url = await serve(registry)
async with Client(url, AdapterSet.default().by_version["2026-07-28"]) as client:
    await client.initialize()
    await client.call_tool("write", {})

assert len(released) == 1
```

The part after `yield` runs whether the handler returned, raised, or was
cancelled by a client that went away.

## The exchange itself

`Exchange` needs no provider. A handler that annotates a parameter with it gets
this request's exchange -- which is how it asks the user, logs, reports
progress, and reaches the session.

<!-- name: test_dependencies -->
```python
from aiohttp_tiny_mcp import Exchange


@registry.tool
async def whoami(args: Nothing, ex: Exchange) -> str:
    """Report who is calling, as their revision stated it."""
    return ex.client_info.name or "anonymous"
```

## What middleware decided

`ex.request` is the aiohttp request, so anything a middleware put there is
reachable. A provider can turn a middleware decision into a typed handler
parameter. For bearer-token verification and an existing aiohttp application's
JWT middleware, see [Authentication](auth.md).

<!-- name: test_dependencies -->
```python
USER: web.RequestKey[str] = web.RequestKey("user", str)


@web.middleware
async def authenticate(request: web.Request, handler):
    request[USER] = request.headers.get("Authorization", "anonymous")
    return await handler(request)


@registry.tool(name="whoami_http")
async def whoami_http(args: Nothing, ex: Exchange) -> str:
    """Read what the middleware decided."""
    return ex.request[USER]
```

A provider can read it too, which keeps the handler from touching the request
at all:

<!-- name: test_dependencies -->
```python
class User(str):
    pass


async def current_user(ex) -> User:
    return User(ex.request[USER])


registry.provide(User, current_user)


@registry.tool(name="greet_user")
async def greet_user(args: Nothing, user: User) -> str:
    """Never sees a request."""
    return f"hello {user}"
```

End to end, with the middleware in place:

<!-- name: async test_dependencies -->
```python
import aiohttp

from aiohttp_tiny_mcp import Endpoint

application = web.Application(middlewares=[authenticate])
Endpoint(registry).setup(application, "/mcp")
runner = web.AppRunner(application)
await runner.setup()
site = web.TCPSite(runner, "127.0.0.1", 0)
await site.start()
host, port = runner.addresses[0]

try:
    headers = {"Authorization": "ada"}
    async with aiohttp.ClientSession(headers=headers) as http:
        adapter = AdapterSet.default().by_version["2026-07-28"]
        async with Client(f"http://{host}:{port}/mcp", adapter, session=http) as client:
            await client.initialize()
            greeting = await client.call_tool("greet_user", {})
            reported = await client.call_tool("whoami_http", {})
finally:
    await runner.cleanup()

assert greeting.content[0].text == "hello ada"
assert reported.content[0].text == "ada"
```

## Missing providers are caught early

A type with no provider is refused when the handler is registered, not when it
is first called.

<!-- name: test_dependencies -->
```python
import pytest


class Missing:
    pass


with pytest.raises(TypeError, match="no provider"):

    @registry.tool
    async def broken(args: Nothing, absent: Missing) -> str:
        """Never reaches a client."""
        return "unreachable"
```

Register providers before the handlers that want them. The plan is checked at
registration, which is what makes this a startup error rather than a surprise
in production.
