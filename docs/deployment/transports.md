# Transports

Streamable HTTP is the primary transport for a remote service: clients connect
to a URL, and the service can run across processes and machines behind a load
balancer. stdio serves clients that launch a local subprocess. Both use the
same registry and handler declarations.

## Streamable HTTP

One endpoint, mounted on an aiohttp application. `routes` returns ordinary
`RouteDef`s, so the endpoint is added the way anything else is:

<!-- name: test_transports -->
```python
from aiohttp import web

from aiohttp_tiny_mcp import Endpoint, MemoryHub, MemorySessionStore, Registry

registry = Registry("service", "1.0", hub=MemoryHub(), session_store=MemorySessionStore())
endpoint = Endpoint(registry)

app = web.Application()
app.add_routes(endpoint.routes("/mcp"))
```

`setup` adds the same routes and returns the application. `app` creates an
application with the same routes:

<!-- name: test_transports -->
```python
endpoint.setup(web.Application(), "/mcp")  # the same routes, added for you
alone = endpoint.app("/mcp")  # ...on an application of its own
```

`POST` carries every request. `GET` opens the stream on which the revisions
before `2026-07-28` read their notifications; `2026-07-28` reads the same events
through `subscriptions/listen` and is told so with a `405`.

A reply is a single JSON body, or a stream of `data:` events where the call has
something to send before its result -- progress, log messages, or a question.

### Concurrent batches

`Endpoint` and `SseEndpoint` accept `batch_concurrency`, a positive integer.
The default, `4`, lets up to four batch items run concurrently while their
handlers await I/O:

<!-- name: test_transports -->
```python
endpoint = Endpoint(registry, batch_concurrency=4)
```

The limit applies separately to each batch, not to all requests on the endpoint.
It does not create threads or make blocking handler code run in parallel.
Only the `2025-03-26` revision accepts JSON-RPC batch arrays; other revisions
still reject them. Single-message requests and stdio behavior do not change.

Streamable HTTP returns the response array in request order after all items
finish. Notifications have no response entries. Invalid items and handler
errors do not prevent other items from running. With HTTP+SSE, replies travel
on the existing stream and can arrive in completion order; match them by `id`.

Concurrent batch items must not depend on each other's side effects or session
updates. Set `batch_concurrency=1` or use separate awaited requests when order matters.

### Access logs

`Endpoint.setup()` and `SseEndpoint.setup()` enable token masking on
`aiohttp.access` by default. `Endpoint.app()` uses the same setup. Normal runners
need no extra arguments:

<!-- name: test_transports -->
```python
app = web.Application()
endpoint.setup(app, "/mcp")
runner = web.AppRunner(app)
```

A logging filter replaces query values for `token`, `access_token`, `secret`,
and `jwt` with `[REDACTED]` in request lines and logged URL headers, including
`Referer`. It also masks names from `Query` objects in a registered policy's
`sources`. See [custom credential sources](../guide/auth.md#custom-credential-sources)
to register additional names. Text and
structured fields are both masked. Repeated and percent-encoded parameter names
are recognized.
The filter preserves the original request, log format, handlers, and logger class.
It also covers refusals before an MCP handler runs.

To skip filter installation, pass `mask_tokens_in_logs=False` at setup:

<!-- name: test_transports -->
```python
app = web.Application()
endpoint.setup(app, "/mcp", mask_tokens_in_logs=False)

standalone = endpoint.app("/mcp", mask_tokens_in_logs=False)
```

`SseEndpoint.setup()` accepts the same option. The logger is shared across the
process, so this opt-out does not remove a filter installed by another setup.
Endpoint construction and low-level `routes()` or `view` registration do not
configure logging.

The filter applies to the process's standard `aiohttp.access` logger, including
other routes logged there. Custom named access loggers, replacement access logger
implementations, proxy logs, and server error logs need their own filtering.

### The event stream

Streams are `SSEResponse`, which frames events the way the HTML event-stream
grammar says: one field per line, a blank line to end an event, and a line
opening with a colon as a comment. Multi-line data becomes one `data` field per
line, and a receiver joins them again.

Pushes go on a queue that one task writes, so a handler never waits for the
network and no comment lands inside an event. A comment goes out every 15
seconds of silence, because a proxy closes a stream that a slow tool has not
written to yet. It is not a disconnect check -- a write to a client that has
gone is buffered and fails only later, so the endpoint watches the connection
separately.

`SSEEvent` is one event as a frozen dataclass, and `read_sse` decodes a
response into those events by the same grammar. The bundled client parses with
it, so one grammar serves both directions.

Streams are gzipped for clients that accept gzip, which is the default.
JSON-RPC repeats its keys, so there is a lot to save, and a client that cannot
decompress does not send `Accept-Encoding: gzip` and so is served plain text.

Compressing here rather than at the edge is the point: a proxy has to buffer to
compress, and a buffered stream is a stalled one. Everything queued at one
moment is written and flushed together with `Z_SYNC_FLUSH`, so a busy stream
compresses well and a quiet one still arrives at once. nginx leaves a response
that already carries `Content-Encoding` alone.

`Endpoint(registry, compress=False)` turns it off, and `SseEndpoint` takes the
same argument.

### Resuming a stream

`2025-03-26` through `2025-11-25` allow optional SSE replay with
`Last-Event-ID`. The endpoint replays events on the `GET` notification stream.
Each event carries its hub id, so replay works on whichever worker takes the
reconnect. An id the hub never issued is ignored and the stream starts from
now. Replay depends on the hub's [event retention](stores.md#event-retention).

The bundled client remembers the latest id in `client.last_event_id` and passes
one back with `stream_notifications(last_event_id=...)`.

On `2026-07-28`, closing a request's SSE response cancels the request.
Retrying starts a new call and can repeat side effects. See
[Streamable HTTP cancellation](https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/streamable-http#cancellation).

The class is exported, and takes events the way `WebSocketResponse` takes
messages, so an application can use it for its own streams:

<!-- name: async test_transports_stream -->
```python
from aiohttp import web

from aiohttp_tiny_mcp import SSEResponse


async def changes(request: web.Request) -> web.StreamResponse:
    # `compress=True` gzips it where the client accepts gzip.
    stream = SSEResponse(heartbeat=15, retry=2000, compress=True)
    await stream.prepare(request)
    await stream.send_json({"state": "building"}, event="status", id="1")
    await stream.send("done", event="status", id="2")
    return stream


assert callable(changes)
```

### Mounting it in an application you already have

Any path works, prefix included. Pass the path the client will use:

<!-- name: test_mounting -->
```python
from aiohttp import web

from aiohttp_tiny_mcp import Endpoint, Registry
from aiohttp_tiny_mcp.console import Console

app = web.Application()  # the one your service already has

registry = Registry("reports", "1.0")
endpoint = Endpoint(registry)
app.add_routes(endpoint.routes("/reports/mcp"))
app.add_routes(Console("/reports/mcp").routes("/reports/console"))

assert sorted(resource.canonical for resource in app.router.resources()) == [
    "/reports/console",  # the page, either way it is addressed
    "/reports/console/",
    "/reports/console/{file}",  # its stylesheet and script
    "/reports/mcp",
]
```

The console is given the endpoint path as well as its own, because the page is
a client: it has to call the endpoint from the browser.

### Under a subapplication

`aiohttp` also offers `add_subapp`, for a section with its own middleware and
lifecycle, and it prefixes every route the subapplication holds. Most of what
is registered here is a route a client follows, so a prefix is exactly right.
Two things are addresses a client is *told*, and those are taken from the
request that asked, so they carry the prefix without being configured for it:
the console prints its own paths into the page, and the HTTP+SSE stream names
the path a client posts messages to.

Authentication metadata must stay at the site root when MCP routes use a
subapplication. See [metadata mounting](../guide/auth.md#where-the-metadata-has-to-be-served)
for `metadata=False`, `metadata_routes()`, and complete examples.

### Remote deployment

Mount the endpoint in your aiohttp service and expose it through your usual
HTTP gateway. Every worker needs the same logical
[session store and hub](stores.md); separate memory instances cannot coordinate
requests across workers.

Configure endpoint protection before deployment. The
[authentication guide](../guide/auth.md) covers Basic, Bearer, and custom
policies through `Registry(auth=...)`. No authentication middleware is required.

For streaming responses, disable proxy buffering and allow connections to stay
open while a tool runs or waits for a user's answer. The endpoint sets
`X-Accel-Buffering: no` on its SSE responses; ensure your proxy configuration
honors it. Set idle timeouts with `ask_timeout_seconds`,
`completion_timeout_seconds`, and long-lived notification streams in mind.

Shared backends allow new requests to reach any worker. An existing HTTP stream
stays on the worker that accepted it and is lost if that worker stops.

### Origin checking

A request that carries an `Origin` header is refused unless the origin is
allowed. This is the DNS-rebinding protection the transport specification asks
for, and it is on by default.

<!-- name: test_transports -->
```python
endpoint = Endpoint(registry, allowed_origins={"https://app.example.com"})
```

An entry may carry wildcards in the host: `*` matches one label and `**` one
or more, while the scheme and any port match literally.

<!-- name: test_transports -->
```python
endpoint = Endpoint(
    registry,
    allowed_origins={
        "https://*.example.com",  # app.example.com, not a.b.example.com
        "https://**.example.com",  # any depth below example.com, not example.com itself
        "https://app.*.example.com",  # app.eu.example.com
    },
)
```

Requests with no `Origin` header -- the normal case for a native client or
another server -- are unaffected either way.

Behind a reverse proxy the server sees `http://` and the proxy's `Host`, so
the origin a browser sends never matches and every request fails with 403
`origin not allowed`. Opt out there and let the proxy check `Origin`:

<!-- name: test_transports -->
```python
endpoint = Endpoint(registry, trust_proxy_origin_validation=True)
```

### Pinning a revision

A client that negotiates badly, or cannot send headers at all, can be pinned
from the URL it is given:

```
https://example.com/mcp?mcp=2025-06-18
```

See [how the pin is applied](../reference/selection.md#the-mcp-pin).

## stdio

Newline-delimited JSON-RPC over a process's standard input and output. This is
how a locally-installed MCP server is usually reached.

Save this as `server.py` and point a host at `python server.py`:

<!-- name: test_transports_stdio; fixtures: __name__ -->
```python
import asyncio

from aiohttp_tiny_mcp import Registry, run_stdio

registry = Registry("service", "1.0")

if __name__ == "__main__":
    asyncio.run(run_stdio(registry))
```

It reads until its input closes, so there is nothing to stop and no port to
choose. The host that launched it owns its lifetime.

Use `run_stdio(registry, resources="tool")` for clients that read data through
[resource tools](../guide/resources.md#access-through-tools). For clients with
native resources but no native prompts, use `run_stdio(registry, prompts="resource")`
to add [prompt addresses](../guide/prompts.md#clients-without-native-prompts)
to the native resource catalogue. `serve_stdio()` accepts the same keywords.
Omit either keyword, or pass `None` or `"native"`, for its native mode.

The tool catalogue includes prompt addresses. With `resources="tool"`, use
that catalogue to find and read prompts. `Registry(resource_tools=False)`
disables both compatibility modes.

It is not a reduced transport. Subscriptions, questions, logging, progress and
cancellation all work there. Two things follow from having one channel instead
of two.

**A call that can be asked something runs concurrently.** It waits for an answer
that only the read loop can deliver, so it must not hold that loop. A call that
cannot be asked keeps its place in line, which is what a client without MRTR
expects.

**What a client states once lives on the connection.** One connection serves one
client for its whole life, so its capabilities and log level need no session to
be remembered in.

What stdio does not have is a second channel. A revision that reads
notifications on a separate stream cannot do so here. Use individual requests:
the stdio server decodes `2025-03-26` batch input but emits separate response
lines, not a batch response array.

Cancellation differs too: HTTP closes the response stream, stdio sends
`notifications/cancelled`.

## HTTP+SSE, for 2024-11-05 clients

Streamable HTTP replaced this in 2025-03-26, which calls it deprecated and
tells a server that wants to serve older clients to keep both. It is not
mounted unless you ask for it:

<!-- name: test_transports -->
```python
from aiohttp_tiny_mcp import SseEndpoint

both = web.Application()
Endpoint(registry).setup(both, "/mcp")
# Endpoint already serves any metadata declared by registry.auth.
SseEndpoint(registry).setup(both, "/sse", "/messages", metadata=False)
```

It works the other way round. The client opens one long stream with `GET /sse`
and is told, in an `endpoint` event, where to post; every message it sends
afterwards is a POST to that address, and every message the server sends --
progress, log lines, results -- goes out on the stream it opened first. The
stream is the session.

Authentication applies separately to `GET /sse` and every `POST /messages`.
See the [authentication guide](../guide/auth.md) for Basic, Bearer, custom
policies, session ownership, and credential headers. The guide also explains
[metadata mounting](../guide/auth.md#where-the-metadata-has-to-be-served)
when both transports share one protected resource.

Nothing pins that POST to the worker holding the stream, so the reply is
published to the hub under a topic named for the connection and written out by
whichever worker holds it. A handler cannot tell which worker it is running on.

Each POST is answered `202` before its handler runs, and the handler's own
reply follows on the stream. The wait would otherwise be a deadlock: a client
of this transport posts one message at a time, including the answer to a
question a handler asked, so a handler that waits for that answer would wait
for a message the client cannot send until the first POST has been answered.
The consequence is that replies arrive in completion order, as they do for a
batch, and are matched by `id`.

**Handler lifetime.** The `202` also ends the POST middleware scope. Middleware
may set identity or other request data before calling the endpoint, but resources
it releases after `await handler(request)` are no longer alive when an MCP
handler uses them. Move transactions and borrowed connections into
[async-generator providers](../guide/dependencies.md#an-async-generator).
Those providers stay open for the handler and close on return, error or cancellation.
Application-wide pools may remain in `app.cleanup_ctx` and be supplied through an
application key. The endpoint cancels remaining background work on shutdown.

The transport is not the revision. It was defined by `2024-11-05`, and that is
what a client is assumed to speak here before it has said otherwise, but the
handshake decides -- the official SDK's own SSE client speaks `2025-11-25` over
it. `2026-07-28` is refused: it removed this transport and requires headers
these messages do not carry.

Whether you need it at all is a fair question. The transport has been
deprecated since March 2025, and a host that reaches remote MCP servers today
speaks Streamable HTTP. Mount it if you know you have such a client, and leave
it out otherwise.

## Which one a client uses

| | Streamable HTTP | stdio |
| --- | --- | --- |
| Remote servers | yes | no |
| Locally-installed servers | sometimes | usually |
| `subscriptions/listen` | yes | yes |
| `GET` notification stream | yes | no second channel |
| Batching on `2025-03-26` | yes | no |
| HTTP+SSE for `2024-11-05` | `SseEndpoint`, opt-in | not applicable |
| Several calls at once | yes | only where one can be asked something |
