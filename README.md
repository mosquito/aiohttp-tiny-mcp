# aiohttp-tiny-mcp

An MCP server and client library for aiohttp, designed for **remote MCP over
HTTP across multiple processes and servers**.

Use it to expose your application's operations and data to assistants: search a
catalog, read a document, or request a deployment. You declare async Python
handlers and Pydantic argument models. The library publishes tool, resource,
and prompt descriptions, validates calls, and handles MCP messages and streams.
It supports five protocol revisions from the same handler declarations, and is
optimized for low-overhead HTTP and stdio operation. See
[performance](#performance) for reproducible measurements and methodology.

## Shared state (optional)

`Registry` uses in-memory sessions and events by default, which is suitable for
tests and one process. For multiple workers, give every worker the same
`SessionStore` and `Hub`: SQLite for workers on one machine, Redis or
PostgreSQL across machines. The backend owns persistence; handlers use
`Exchange` (`ex.session`, `ex.ask`, progress) without knowing which backend is
in use.

For a local multi-process setup:

<!-- name: test_readme_sqlite -->
```python
from aiohttp import web

from aiohttp_tiny_mcp import Registry
from aiohttp_tiny_mcp.sqlite import SqliteHub, SqliteSessionStore, SqliteStorage

storage = SqliteStorage("mcp.sqlite")
registry = Registry(
    "service", "1.0", hub=SqliteHub(storage), session_store=SqliteSessionStore(storage)
)

app = web.Application()
# Open the file for the application's lifetime and sweep expired rows.
app.cleanup_ctx.append(storage.cleanup_ctx)
```

For Redis/PostgreSQL setup, backend parameters, cleanup, and deployment
constraints, see [stores and hubs](docs/deployment/stores.md).

## Start a server

Python 3.10+ is required. Runtime dependencies are `aiohttp` and `pydantic`.

```bash
pip install aiohttp-tiny-mcp
```

For a first runnable server and client, follow the [quickstart](docs/quickstart.md).
The example below adds a resource and a tool that asks for confirmation. Save
it as `server.py`. The deployment result is illustrative; replace it with your
application's operation.

<!-- name: test_readme_server; fixtures: __name__ -->
```python
from aiohttp import web
from pydantic import BaseModel

from aiohttp_tiny_mcp import (
    Endpoint,
    Exchange,
    Registry,
    elicit,
)

registry = Registry("demo", "0.1.0")


class Nothing(BaseModel):
    pass


class Deploy(BaseModel):
    service: str


@registry.resource("config://app", mime_type="application/json")
async def config(args: Nothing) -> dict:
    """Application configuration."""
    return {"debug": False}


@registry.tool
async def deploy(args: Deploy, ex: Exchange) -> str:
    """Deploy a service, once somebody agrees to it."""
    agreed = await ex.ask("confirm", elicit(f"Deploy {args.service}?"))
    if not agreed.accepted:
        return f"stopped at {agreed.action}"
    return f"deployed {args.service}"


app = Endpoint(registry).app("/mcp")

if __name__ == "__main__":
    web.run_app(app, host="127.0.0.1", port=8080)
```

Run it locally:

```bash
python server.py
```

To try it without a client, mount the console beside the endpoint:

<!-- name: test_readme_console; fixtures: app -->
```python
from aiohttp_tiny_mcp.console import Console

Console("/mcp", title="Demo").setup(app, "/console")
```

Open `http://127.0.0.1:8080/console`. It speaks the protocol itself on any of
the five revisions, builds a form from each tool's schema, answers the
questions a handler asks, and shows every message either way. Three files from
this package, no build step and no second process.

An MCP host that supports Streamable HTTP can connect to
`http://127.0.0.1:8080/mcp`. In an existing aiohttp service, use
`Endpoint(registry).setup(app, "/mcp")`. For a local subprocess transport,
`run_stdio(registry)` serves the same declarations.

The `Deploy` model becomes the tool's input schema; the function name and
docstring become its name and description. `ex: Exchange` is supplied by the
library, so the caller only supplies `service`. `ex.ask` requests a decision
from the client. Put irreversible work after the final question: some revisions
restart the handler when the answer arrives. Python locals are not persisted
automatically. See [Asking the user](docs/guide/asking.md).

## Call it from Python

The bundled `Client` is useful for integration tests or an application that
connects to MCP servers. The following runs inside an async function with `url`
set to your endpoint URL. Its callback automatically accepts the question;
in an interactive application, collect the user's answer there.

<!-- name: async test_readme_client; fixtures: url, readme_events -->
```python
from aiohttp_tiny_mcp import Client, elicit_accept
from aiohttp_tiny_mcp.protocol.selection import AdapterSet


async def answer(request):
    return elicit_accept({"ok": True})


adapter = AdapterSet.default().by_version["2025-06-18"]
async with Client(url, adapter, on_ask=answer, log_level="info") as client:
    await client.initialize()
    result = await client.call_tool("deploy", {"service": "web"})

    async for change in client.listen(resources=["config://app"]):
        print(change["params"]["uri"])
        break
```

The subscription loop waits for a resource-change event. The server snippet
above does not publish changes; see [Notifications](docs/guide/notifications.md)
for that part, or omit the loop when testing only the tool call.
`StdioClient` provides the corresponding client over a subprocess's stdin/stdout.

## Performance

The project includes reproducible HTTP and stdio benchmarks against the
official SDK. Results depend on Python, hardware, and protocol revision; see
[the benchmark methodology and full results](docs/reference/benchmarks.md)
instead of treating a README number as a guarantee.

## Documentation

Start with [the documentation overview](docs/index.md), then follow:

1. [Tools, resources, and prompts](docs/concepts.md): what to expose and what the client sees.
2. [Quickstart](docs/quickstart.md): a complete server, launch command, and client call.
3. [How the server fits together](docs/pieces.md): a conversation across two workers and each object's lifetime.
4. [Using Exchange](docs/guide/exchange.md): request context, progress, questions, and state.
5. [Authentication](docs/guide/auth.md): bearer-token verification and OAuth resource metadata.
6. [Stores and hubs](docs/deployment/stores.md): shared backend contracts and deployment requirements.

The server supports `2026-07-28`, `2025-11-25`, `2025-06-18`, `2025-03-26`
and `2024-11-05`.
Delivery mechanisms and client support differ; see the
[compatibility table](docs/reference/parity.md) and
[implementation coverage](docs/reference/verification.md#not-implemented).

Documentation examples are checked by the test suite. From a source checkout:

```bash
uv run pytest docs README.md
uv run --group docs sphinx-build -W -b html docs docs/_build
```
