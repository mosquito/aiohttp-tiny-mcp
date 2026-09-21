# How the server fits together

You create a `Registry`, register handlers, and mount an `Endpoint`. The
library creates an `Exchange` each time it invokes a handler.

| Object | Job |
| --- | --- |
| `Registry` | Holds tools, resources, prompts, and dependency providers |
| `Endpoint` | Receives MCP HTTP requests and dispatches them |
| `Exchange` | Lets one running handler talk to its caller |
| `SessionStore` | Keeps records that another request or worker needs |
| `Hub` | Delivers replies and notifications between workers |

```{mermaid}
flowchart TD
    client[MCP client] --> endpoint[Endpoint]
    endpoint --> registry[Registry]
    registry --> handler[Your handler]
    endpoint <--> store[SessionStore]
    handler --> exchange[Exchange]
    exchange <--> store
    exchange <--> hub[Hub]
```

## The ordinary call

For `@registry.tool async def add(args: Add) -> Sum`, the endpoint validates
the client's JSON as `Add`, calls the handler, and sends its return value as an
MCP tool result. No `Exchange` is needed.

Add `ex: Exchange` when the handler needs the current call rather than just
its arguments:

```python
@registry.tool(streaming=True)
async def deploy(args: Deploy, ex: Exchange) -> str:
    await ex.progress(1, 2, "Checking")
    answer = await ex.ask("confirm", elicit(f"Deploy {args.service}?"))
    if not answer.accepted:
        return "Cancelled"
    return "Deployed"
```

The client sends only `service`; `ex` is injected by the library. It binds
progress, log messages, questions, session access, cancellation, and the
response stream to this invocation. Do not retain it after the handler returns.
The complete API is in [Using Exchange](guide/exchange.md).

## Package layout

Import the main classes from `aiohttp_tiny_mcp`. Their implementations are
grouped by responsibility:

| Package | Contents |
| --- | --- |
| `server` | Registration, dispatch, handler context, HTTP and stdio endpoints |
| `client` | Shared client logic, HTTP and stdio clients |
| `protocol` | Wire models, schemas, adapters, and protocol revisions |
| `storage` | Session stores, event hubs, namespaces, and database backends |
| `auth` | Authentication policies and optional JWT verification |
| `oauth` | OAuth providers, authorization server, and tokens |
| `extensions` | Extension declarations and Skills |
| `transport` | Shared HTTP routing and SSE framing |
| `console` | Browser console and its assets |

`aiohttp_tiny_mcp.testing` provides test clients. Optional backends use explicit
imports, such as `aiohttp_tiny_mcp.storage.postgres` and
`aiohttp_tiny_mcp.auth.jwt`.

## Why Store and Hub exist

They matter when requests can land on different workers.

```{mermaid}
sequenceDiagram
    participant A as Worker A
    participant S as Shared Store
    participant B as Worker B
    A->>S: Save session or question state
    B->>S: Load it on the next request
```

The store keeps protocol sessions, application session values, and explicit
state left before a handler asks a question. It is not your application
database; persist business data there as usual.

```{mermaid}
sequenceDiagram
    participant A as Worker A
    participant H as Shared Hub
    participant B as Worker B
    A->>H: Capture cursor, then ask client
    B->>H: Publish reply received on B
    A->>H: Poll after cursor
    H-->>A: Reply
```

The hub is an event log. It lets a question sent from A receive a reply that
arrived on B; it also distributes resource-change notifications. Progress and
logs travel directly on the active response stream, so they do not use the hub.

For a single process, `Registry` uses `MemorySessionStore()` and `MemoryHub()`
by default. For multiple workers, all workers need shared implementations of
both contracts.
The exact methods, expiry rules, and backend guidance are in
[Stores and hubs](deployment/stores.md).

## One important lifetime rule

Some MCP revisions keep `await ex.ask(...)` on the current stream. Others end
the call and invoke the handler again when the answer arrives. The second call
may run on another worker with a new exchange. Put irreversible work after the
final question. If a computed value must survive, pass JSON-serializable state
through `NeedInput(..., state=...)`; see [Asking the user](guide/asking.md).
