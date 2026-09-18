# Tools, resources, and prompts

MCP connects a client — usually an application hosting a model — to your
server. Your server offers one or more of these:

| Use this when you offer… | Declare | Example |
| --- | --- | --- |
| An operation with arguments | A tool | Search a catalog; create a deployment |
| Read-only content at an address | A resource | `catalog://items/42` |
| A reusable conversation starting point | A prompt | “Review this incident” |

A tool is the default. Its Pydantic input model becomes an input schema; its
docstring tells a client when to call it. A resource is addressed by a URI,
not called with arbitrary arguments. A prompt returns messages for a client to
show or use in a conversation.

```python
@registry.tool
async def search(args: Search) -> Results:
    """Search the catalog by text."""
    ...
```

See [Tools](guide/tools.md), [Resources](guide/resources.md), and
[Prompts](guide/prompts.md) for the APIs and result types.

## What you declare

The server's public surface consists of a registry and its handlers:

| Declare | The client receives |
| --- | --- |
| `Registry(name, version, ...)` | Server identity and instructions |
| Tool function, model, and docstring | Name, description, input schema, and result |
| Resource URI and handler | A readable URI or URI template |
| Prompt function and model | A named prompt and its arguments |
| `registry.extension(extension)` | Extension capabilities and methods, with resources for older clients |

`Exchange`, database clients, the store, and the hub are server-side objects.
They are never tool arguments and are not visible in a tool listing.

## MCP and this library

MCP defines the messages on the wire: JSON-RPC, tools, resources, prompts,
transports, capabilities, and notifications. This library maps those messages
to aiohttp and Python:

| MCP defines | This library supplies |
| --- | --- |
| Tool/resource/prompt messages | Pydantic handlers and decorators |
| Streamable HTTP and stdio | `Endpoint` and `run_stdio` |
| Progress, logging, and elicitation | `Exchange` methods |
| Sessions and notifications | `SessionStore` and cursor-based `Hub` contracts |
| Protocol revisions | Five adapters behind one handler API |

The protocol does not choose a database, persist a Python session, or deliver
an event between two application workers. That is the remote-web-mcp problem
this library addresses. [How the server fits together](pieces.md) explains the
two contracts used for it.

(official-python-sdk)=
## Official Python SDK

The official [Python SDK](https://github.com/modelcontextprotocol/python-sdk)
already provides a high-level server API: `FastMCP` in 1.x and `MCPServer` in
2.x. Its broad feature set is useful.

Both serve the same five protocol revisions from one handler set.

| Revisions | the same five |
| --- | --- |
| Covered dates | `2024-11-05` through `2026-07-28` |

### Why this library exists

For a server behind a load balancer, the SDK has the wrong state boundary.
Its **stateful** Streamable HTTP session belongs to the process that created
it. A request routed to another worker gets `Session not found`: a stateful HTTP session does not move,
and the SDK exposes no backend seam for sharing it.

This is not imposed by JSON-RPC or by Streamable HTTP. A JSON-RPC request is
self-contained; for the older MCP revisions, its session ID can simply select
a shared record. Keeping the transport session in one worker's memory is an
SDK design choice. It turns stateless serving into a special mode rather than
the normal deployment model.

The SDK can run **stateless** HTTP across workers, but this is an escape hatch:
it gives up the server-side session. This is sufficient for `2026-07-28`, which
has no handshake session. It is not sufficient for a cross-protocol server that
must serve the older revisions with negotiated capabilities, log level, and
subscriptions without sticky routing. The alternatives are affinity, which
defeats ordinary load balancing, or dropping that state.

The SDK can distribute question state and change events. Its question state is
encrypted and carried by the client; workers share a key. A shared pub/sub can
fan change events out across replicas. Neither mechanism provides shared
legacy sessions.

This library puts all three concerns in application backends: `SessionStore`
holds legacy sessions and opaque request state, and `Hub` carries replies and
notifications. A handler keeps the same API on all five revisions, while any
worker can handle the next request; nothing leaves the server except opaque
identifiers.

The SDK is also disproportionately slow before application work begins. The
local benchmark normalizes each HTTP stack against its bare framework and still
measures about 3–4× the `tools/call` rate here; over stdio, where no web
framework is involved, it is about 4–5×. Its stateless mode is slower than its
stateful mode on legacy revisions because it creates and destroys a session for
each request. These are small-handler measurements, not production capacity
claims; see [What it costs](reference/benchmarks.md).

### Choose deliberately

Choose the SDK when its sampling or roots APIs are required, or when
Starlette/ASGI is a fixed constraint. This library supports aiohttp middleware,
[custom extensions and skill directories](guide/extensions.md), and bearer-token
verification for an OAuth protected resource; see [Authentication](guide/auth.md).
Choose it for aiohttp and for legacy session-based MCP behind a load balancer.
The distributed claims are exercised in `tests/test_cluster.py`.
