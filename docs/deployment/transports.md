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

`setup` adds the same routes and returns the application, and `app` calls it on
a new one. They are shorthands for the line above, not another code path:

<!-- name: test_transports -->
```python
endpoint.setup(web.Application(), "/mcp")  # the same routes, added for you
alone = endpoint.app("/mcp")  # ...on an application of its own
```

Every one of these logs the paths it registers at `DEBUG` on the
`aiohttp_tiny_mcp` logger, including the ones you did not name yourself.

`POST` carries every request. `GET` opens the stream on which the revisions
before `2026-07-28` read their notifications; `2026-07-28` reads the same events
through `subscriptions/listen` and is told so with a `405`.

A reply is a single JSON body, or a stream of `data:` events where the call has
something to send before its result -- progress, log messages, or a question.

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

`2025-03-26` through `2025-11-25` let a server number its SSE events and a
client that lost the connection ask, with `Last-Event-ID`, for what followed.
The `GET` stream does this. Each event carries the id the hub gave it, so the
replay is a poll from that exact id and works on whichever worker takes the
reconnect. An id the hub never issued is ignored and the stream starts from
now. How far back a replay can reach is the hub's retention: `MemoryHub(keep=)`
counts events, the storage backends keep theirs until they expire.

The bundled client remembers the latest id in `client.last_event_id` and passes
one back with `stream_notifications(last_event_id=...)`.

The stream a `POST` opens for one request carries no ids and cannot resume.
Retrying the request can execute the handler again. Applications must make
retries safe when handlers have side effects. `2026-07-28` has no `GET` stream;
it returns `405` and ignores `Last-Event-ID`.

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
honors it. Set idle timeouts with `ask_timeout_seconds` and long-lived
notification streams in mind.

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

The query wins over anything in the body or the headers, deliberately: it
states what the deployment intends this client to speak.

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
