# Quickstart

Build a server that lets a client save a short note. Then discover and call its
tool from Python. No language model, API key, or external database is needed
for this example. The library is designed for remote HTTP deployments with
multiple workers; here both synchronization backends live in one process so
you can learn the API first. The same handlers can later use shared backends.

## Install

Use Python 3.10 or newer, preferably in a virtual environment:

```bash
pip install aiohttp-tiny-mcp
```

The runtime dependencies are `aiohttp` and `pydantic`.

## Define the server

Save this as `server.py`:

<!-- name: test_quickstart; fixtures: __name__ -->
```python
from aiohttp import web
from pydantic import BaseModel, Field

from aiohttp_tiny_mcp import Endpoint, Registry

registry = Registry(
    "notes",
    "0.1.0",
    instructions="Keeps short notes for this server process.",
)

NOTES: list[str] = []


class Note(BaseModel):
    text: str = Field(min_length=1, description="The note to save.")


class Count(BaseModel):
    total: int = Field(description="Number of notes stored after this call.")


@registry.tool
async def remember(args: Note) -> Count:
    """Save a short note and return the number of stored notes."""
    NOTES.append(args.text)
    return Count(total=len(NOTES))


app = Endpoint(
    registry,
    # Behind a reverse proxy, uncomment this and let the proxy check Origin.
    # trust_proxy_origin_validation=True,
).app("/mcp")

if __name__ == "__main__":
    web.run_app(app, host="127.0.0.1", port=8080)
```

Here is what each declaration does:

| Declaration | What it means |
| --- | --- |
| `Registry("notes", "0.1.0", ...)` | Identifies your server and collects its handlers; `0.1.0` is your application's version |
| `Note` | Describes and validates arguments the client sends |
| `Count` | Describes the structured result the client receives |
| `@registry.tool` | Exposes `remember` as a discoverable, callable MCP tool |
| `Endpoint(...).app("/mcp")` | Creates an aiohttp application with MCP routes at `/mcp` |

The client sees the tool name `remember`, its docstring, an input schema with a
required `text` field, and an output schema with `total`. It does not see the
registry, hub, store, or Python implementation. The library builds the schemas
from your models; you do not write JSON-RPC handlers or a separate tool manifest.

The registry creates a `MemorySessionStore` and `MemoryHub` when they are not
passed. They work within this process and disappear on restart. The `NOTES`
list is separate application data; for a real notes service, replace it with
your application's storage.

Before exposing the endpoint beyond a trusted network, configure
[Authentication](guide/auth.md).

## Run it

In the directory containing `server.py`:

```bash
python server.py
```

Leave it running. The MCP URL is `http://127.0.0.1:8080/mcp`. This is a protocol
endpoint; opening it in a browser is not a test of the `remember` tool. Use the
client below, or configure an MCP host that supports Streamable HTTP with this
URL. For a browser interface, see [Console](guide/console.md).

If you already have an aiohttp application, use
`Endpoint(registry).setup(existing_app, "/mcp")` instead of `.app(...)`.
For a client that launches a local subprocess, see [stdio](deployment/transports.md#stdio).

## Discover and call the tool

Save this as `client.py`:

<!-- name: test_quickstart; fixtures: __name__ -->
```python
import asyncio

from aiohttp_tiny_mcp import Client
from aiohttp_tiny_mcp.protocol.selection import AdapterSet


async def main(url: str = "http://127.0.0.1:8080/mcp"):
    adapter = AdapterSet.default().by_version["2025-11-25"]
    async with Client(url, adapter) as client:
        await client.initialize()
        tools = await client.list_tools()
        print([tool.name for tool in tools])
        result = await client.call_tool("remember", {"text": "milk"})
        print(result.structured_content)
        return tools, result


if __name__ == "__main__":
    asyncio.run(main())
```

In a second terminal, run:

```bash
python client.py
```

On the first call after starting the server, the output is:

```text
['remember']
{'total': 1}
```

Each subsequent call adds another note. `initialize()` establishes the protocol
conversation, `list_tools()` discovers declarations, and `call_tool()` invokes
the handler with JSON arguments. The bundled client explicitly selects a
protocol adapter; the server can serve all its supported revisions from the
same registry.

The documentation test runs this client against a temporary HTTP server and
checks the result:

<!-- name: async test_quickstart; fixtures: serve -->
```python
url = await serve(registry)
tools, result = await main(url)
assert [tool.name for tool in tools] == ["remember"]
assert result.structured_content == {"total": 1}
```

## Add the next feature

You now have the complete declaration and call path. You can keep a handler
this small: an `Exchange`, resource, prompt, and dependency provider are all
optional until the operation needs them.

- To expose more operations, follow [Tools](guide/tools.md).
- To offer documents or reusable message templates, use
  [Resources](guide/resources.md) or [Prompts](guide/prompts.md).
- To use an existing database client in handlers, see
  [Dependencies](guide/dependencies.md).
- To report progress or ask for a decision, see [Using Exchange](guide/exchange.md).
- To understand object lifetimes and the two backends, continue with
  [How the server fits together](pieces.md).
