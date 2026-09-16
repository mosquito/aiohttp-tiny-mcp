# The client

`Client` speaks one revision -- whichever adapter it is given -- and the calling
code is the same for all of them. `StdioClient` is the same class over a
subprocess's standard streams.

<!-- name: async test_client; fixtures: serve -->
```python
from pydantic import BaseModel

from aiohttp_tiny_mcp import Client, Registry
from aiohttp_tiny_mcp.protocol.selection import AdapterSet

registry = Registry("demo", "1.0")


class Add(BaseModel):
    a: int
    b: int


class Sum(BaseModel):
    total: int


@registry.tool
async def add(args: Add) -> Sum:
    """Add two integers."""
    return Sum(total=args.a + args.b)


url = await serve(registry)
adapter = AdapterSet.default().by_version["2026-07-28"]

async with Client(url, adapter) as client:
    await client.initialize()
    tools = await client.list_tools()
    result = await client.call_tool("add", {"a": 2, "b": 3})

assert [tool.name for tool in tools] == ["add"]
assert result.structured_content == {"total": 5}
```

`initialize` must come first. On a revision with a handshake it is the
handshake; on `2026-07-28` it is `server/discover`, and the client sends what
that revision expects instead.

## Choosing a revision

`AdapterSet.default()` holds every revision this package speaks.

<!-- name: async test_client -->
```python
versions = [adapter.version for adapter in AdapterSet.default().adapters]
assert versions == [
    "2026-07-28",
    "2025-11-25",
    "2025-06-18",
    "2025-03-26",
    "2024-11-05",
]
```

There is no negotiation on the client side: pick the newest the server supports,
or the one you mean to test. A server that cannot speak it refuses and names
what it does support, rather than leaving the client to guess.

A server restricted to the older revisions is built by giving the endpoint a
narrower set:

<!-- name: async test_client_version; fixtures: serve -->
```python
import pytest
from aiohttp import web

from aiohttp_tiny_mcp import Client, ClientError, Endpoint, Registry
from aiohttp_tiny_mcp.protocol.selection import AdapterSet
from aiohttp_tiny_mcp.protocol.v2025_11_25 import Adapter2025_11_25

registry = Registry("demo", "1.0")

legacy_only = AdapterSet([Adapter2025_11_25()])
runner = web.AppRunner(Endpoint(registry, adapters=legacy_only).app("/mcp"))
await runner.setup()
site = web.TCPSite(runner, "127.0.0.1", 0)
await site.start()
host, port = runner.addresses[0]

try:
    async with Client(
        f"http://{host}:{port}/mcp", AdapterSet.default().by_version["2026-07-28"]
    ) as client:
        with pytest.raises(ClientError) as raised:
            await client.initialize()
finally:
    await runner.cleanup()

assert raised.value.data["supported"] == ["2025-11-25"]
assert raised.value.data["requested"] == "2026-07-28"
```

The refusal is rendered by the newest revision the *server* speaks, since the
one the client asked for is exactly what it does not have. A server that speaks
`2026-07-28` uses that revision's `-32022`; the one above has only
`2025-11-25`, whose vocabulary has no such code, so it answers `-32600`. The
`data` is the same either way, and is the part worth reading.

## What the constructor takes

<!-- name: async test_client_options; fixtures: serve -->
```python
from pydantic import BaseModel

from aiohttp_tiny_mcp import Client, Registry, elicit_accept
from aiohttp_tiny_mcp.models import Implementation
from aiohttp_tiny_mcp.protocol.selection import AdapterSet

registry = Registry("demo", "1.0")

notifications = []


async def answer(request):
    """Answer a question the server asks. Also declares that it can be."""
    return elicit_accept({})


async def note(frame):
    """Every notification the server sends, progress and logging included."""
    notifications.append(frame["method"])


url = await serve(registry)
client = Client(
    url,
    AdapterSet.default().by_version["2025-11-25"],
    client_info=Implementation(name="my-agent", version="2.0"),
    on_ask=answer,
    on_notification=note,
    log_level="info",
)
```

`on_ask` is the only one that changes what the server does: a server puts a
question only to a client that declared it can answer, and passing `on_ask` is
that declaration. See [Asking the user](asking.md#the-clients-side).

## Calls

<!-- name: async test_client_calls; fixtures: serve -->
```python
from pydantic import BaseModel

from aiohttp_tiny_mcp import Client, Registry
from aiohttp_tiny_mcp.protocol.selection import AdapterSet

registry = Registry("demo", "1.0")


class Nothing(BaseModel):
    pass


class Ref(BaseModel):
    id: int


class Lang(BaseModel):
    language: str


@registry.tool
async def ping(args: Nothing) -> str:
    """Answer."""
    return "pong"


@registry.resource("config://app", mime_type="application/json")
async def config(args: Nothing) -> dict:
    """Configuration."""
    return {"debug": False}


@registry.resource("catalog://items/{id}", name="item")
async def item(args: Ref) -> str:
    """One item."""
    return f"item-{args.id}"


@registry.prompt
async def greet(args: Lang) -> str:
    """A greeting."""
    return f"Say hello in {args.language}."


url = await serve(registry)
async with Client(url, AdapterSet.default().by_version["2025-06-18"]) as client:
    await client.initialize()

    assert {tool.name for tool in await client.list_tools()} == {"ping"}
    assert (await client.call_tool("ping", {})).content[0].text == "pong"

    assert {r.uri for r in await client.list_resources()} == {"config://app"}
    assert "item-7" in (await client.read_resource("catalog://items/7")).contents[0].text

    assert {p.name for p in await client.list_prompts()} == {"greet"}
    prompt = await client.get_prompt("greet", {"language": "en"})
    assert "en" in prompt.messages[0].content.text
```

`listen` is covered in [Notifications](notifications.md#changes-to-what-the-server-offers),
`call_tool`'s question handling in [Asking the user](asking.md).

## Errors

A protocol-level failure raises `ClientError`, carrying the JSON-RPC code and
data. A tool that raised, or was called with bad arguments, does not: it comes
back as a result with `is_error` set, because the call reached the tool.

<!-- name: async test_client_errors; fixtures: serve -->
```python
import pytest
from pydantic import BaseModel

from aiohttp_tiny_mcp import Client, ClientError, Registry
from aiohttp_tiny_mcp.protocol.selection import AdapterSet

registry = Registry("demo", "1.0")


class Add(BaseModel):
    a: int
    b: int


@registry.tool
async def add(args: Add) -> int:
    """Add two integers."""
    return args.a + args.b


url = await serve(registry)
async with Client(url, AdapterSet.default().by_version["2026-07-28"]) as client:
    await client.initialize()

    # The tool was reached and the arguments were wrong.
    bad = await client.call_tool("add", {"a": "not a number"})
    assert bad.is_error is True

    # The tool does not exist, which is a protocol failure.
    with pytest.raises(ClientError):
        await client.call_tool("absent", {})
```

## Over stdio

`StdioClient.spawn` starts a subprocess and talks to its standard input and
output. Its standard error is left alone, so the server's own logging does not
collide with the protocol stream.

Given a server written as in [stdio](../deployment/transports.md#stdio), this
is the whole of a client for it:

<!-- name: async test_client_stdio; fixtures: __name__, stdio_server -->
```python
import asyncio
import sys

from aiohttp_tiny_mcp.protocol.selection import AdapterSet
from aiohttp_tiny_mcp.stdio_client import StdioClient


async def main(script: str):
    adapter = AdapterSet.default().by_version["2026-07-28"]
    async with StdioClient.spawn(sys.executable, script, adapter=adapter) as client:
        await client.initialize()
        result = await client.call_tool("add", {"a": 2, "b": 3})
        print(result.content[0].text)
        return result


if __name__ == "__main__":
    asyncio.run(main("server.py"))
```

Run against the server this page's tests build, it answers `5`:

<!-- name: async test_client_stdio -->
```python
result = await main(stdio_server)
assert result.content[0].text == "5"
```

Everything above works there except `listen` on a revision that reads
notifications on a separate stream: stdio has only one channel, and the client
says so rather than pretending. See [Transports](../deployment/transports.md).
