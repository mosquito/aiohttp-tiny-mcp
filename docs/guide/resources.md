# Resources

A resource is something to read, named by a URI. Reading it changes nothing,
and it takes no arguments beyond what the URI itself carries. See
[Tools, resources, and prompts](../concepts.md) for why that matters: the host decides
what to put in front of the model, so a resource is context rather than an
action.

Not every agent harness supports native MCP resources. A harness can connect
and call tools without making resources available to the model. Support can
also differ between versions, plugins, and resource features such as URI templates.
If your agent cannot discover or read resources, use
[resource tools](#access-through-tools) to expose them through ordinary MCP tool calls.

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

## Access through tools

Use this mode when your agent harness supports MCP tools but cannot use native
resources. The agent can list and read the same resources through two tools;
both tools use the existing resource handlers and permission checks.

Add `resources=tool` to the MCP endpoint URL in the agent's configuration:

```text
https://mcp.example.com/mcp?resources=tool
```

If the URL already has other query parameters, append `&resources=tool`.
Replace existing `resources` values when switching modes. In the
[Add agent dialog](console.md#add-an-agent), select **Resources: as tools**.
Select **native** as well if you also need the native interface. Native resources
remain the default when no mode is specified.

HTTP clients can select a resource interface with the `resources` query parameter.
The setting applies to each request, including discovery and notification streams.

| Endpoint query | Resource interface |
| --- | --- |
| No `resources` parameter | Native MCP resources |
| `?resources=native` | Native MCP resources |
| `?resources=tool` | Resource tools |
| `?resources=tool&resources=native` | Both interfaces |

Repeated values are accepted. Empty or unknown values return HTTP 400.
Other endpoint query parameters are preserved. On legacy HTTP+SSE, the advertised
POST URL carries the selected modes. Stdio uses native resources.

Tool mode exposes two names through `tools/list`:

- `mcp_resources_list(kind="resources", cursor=None)` lists fixed resources.
  Set `kind="templates"` to list URI templates. Pass `nextCursor` back as `cursor`.
- `mcp_resources_read(uri)` reads a concrete URI, including an expanded template.

Both tools return JSON in ordinary text content, so the client does not need to
interpret resource content blocks. Read results retain URI, MIME type, and text
or base64 blob data. The same resource handlers, dependency providers, scope checks,
revision-specific URIs, and pagination rules apply. Tool failures return `isError`.

<!-- name: async test_resources; fixtures: serve -->
```python
import json

async with Client(url + "?resources=tool", AdapterSet.default().by_version["2026-07-28"]) as client:
    await client.initialize()
    result = await client.call_tool("mcp_resources_read", {"uri": "notebook://entries/42"})
    contents = json.loads(result.content[0].text)["contents"]

assert contents[0]["text"] == "entry 42"
```

In tool-only mode, the server omits the native `resources` capability and rejects
native resource listing, reading, subscription, and completion requests. Resource
notifications are disabled. Use both modes when the client needs subscriptions.

`Registry` reserves exactly `mcp_resources_list` and `mcp_resources_read` by default.
Registering either as an application tool raises `ValueError`. Disable the feature
to release both names and stop interpreting the `resources` query parameter:

<!-- name: test_resource_tools_opt_out -->
```python
from aiohttp_tiny_mcp import Registry

registry = Registry("notebook", "1.0", resource_tools=False)
```

With this opt-out, resources always use native methods and no resource tools are
installed. Pass the same setting to `Console(resource_tools=registry.resource_tools)`
to disable the compatibility URLs in generated agent configurations.

## Required scopes

Use `scopes` to restrict reads and subscriptions to verified callers with every
required permission. The same parameter works for fixed resources and templates:

<!-- name: test_resource_scopes -->
```python
from pydantic import BaseModel

from aiohttp_tiny_mcp import Registry, StaticBasicAuth

secured = Registry(
    "files",
    "1.0",
    auth=StaticBasicAuth(("reader", "example-password", ["files:read"])),
)


class NoArguments(BaseModel):
    pass


class File(BaseModel):
    id: int


@secured.resource("files://index", scopes={"files:read"})
async def index(args: NoArguments) -> str:
    return "File index"


@secured.resource("files://entries/{id}", scopes={"files:read"})
async def file(args: File) -> str:
    return f"File {args.id}"
```

Configure `Registry(auth=...)` before registering a resource with nonempty
scopes. Registration fails without an authentication policy. Omitted or empty
scopes add no resource-specific requirement; endpoint authentication still applies.

Scope checks run before the handler and its dependency providers. Resources and
templates remain visible in their listing methods, including to callers who lack
their scopes. Listing a resource does not authorize reading or subscribing to it.
`Extension.resource(..., scopes=...)` applies the same requirements to canonical
and legacy resource URIs. Record-level permissions remain application logic;
see [Handler permissions](auth.md#handler-permissions).

Denied reads, legacy subscriptions, and resource-template completions return
JSON-RPC error `-32003`. Ordinary HTTP JSON responses use status `403`; errors
on an already open stream remain protocol replies. Modern subscription
acknowledgements omit resources the caller cannot access. Notification streams
also check resource scopes before relaying updates from saved subscriptions.
Unsubscribe remains available without the resource's scopes.

Resources with scopes cannot declare `cache_scope="public"`. Use private
caching, which is the default, to keep their contents scoped to the caller.

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
from aiohttp_tiny_mcp.storage.hub import NOTIFICATIONS, topic


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
