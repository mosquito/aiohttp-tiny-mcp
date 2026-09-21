# The normalized core

The types between the adapters and everything else. They are revision-neutral by
construction: a field that only makes sense in one revision does not belong
here.

## `Operation`

The verbs a server can perform. Protocol method names are an adapter concern;
`Operation` is the internal identity.

<!-- name: test_runtime -->
```python
from aiohttp_tiny_mcp.protocol.core import Operation

assert Operation.CALL_TOOL.value == "call_tool"
```

| Operation | Reached by |
| --- | --- |
| `DESCRIBE` | `server/discover`, or `initialize` |
| `HANDSHAKE_COMPLETE` | `notifications/initialized` |
| `PING` | `ping` |
| `LIST_TOOLS`, `CALL_TOOL` | `tools/list`, `tools/call` |
| `LIST_RESOURCES`, `LIST_RESOURCE_TEMPLATES`, `READ_RESOURCE` | the `resources/*` methods |
| `LIST_PROMPTS`, `GET_PROMPT` | the `prompts/*` methods |
| `COMPLETE` | `completion/complete` |
| `LISTEN` | `subscriptions/listen`, on `2026-07-28` only |
| `SUBSCRIBE`, `UNSUBSCRIBE` | `resources/subscribe` and its opposite, on the older revisions only |
| `SET_LOG_LEVEL` | `logging/setLevel`, on the older revisions only |
| `EXTENSION` | A registered extension request on `2026-07-28`; `Call.target` holds its method name |

An adapter may decline to expose an operation, and may map two method names onto
one operation.

<!-- name: test_runtime -->
```python
from aiohttp_tiny_mcp.protocol.selection import AdapterSet

modern = AdapterSet.default().by_version["2026-07-28"]
legacy = AdapterSet.default().by_version["2025-11-25"]

assert modern.method_for(Operation.LISTEN) == "subscriptions/listen"
assert legacy.method_for(Operation.LISTEN) is None
assert legacy.method_for(Operation.SUBSCRIBE) == "resources/subscribe"
assert modern.method_for(Operation.SUBSCRIBE) is None
```

## `Call`

One decoded request, with the revision's differences already flattened. A POST
yields zero of them for a notification the server ignores, one normally, and
several for a batch.

It carries the operation, the JSON-RPC id (or `None` for a notification), the
target, the arguments, the validated params model, who is calling, the progress
token, the log level, any answers already given, and the state left by a
previous attempt.

`client` is filled from `initialize` on a revision with a handshake and from
`_meta` on `2026-07-28`. The dispatcher cannot tell which, which is the point.

## `Outcome`

What a dispatched operation produced, before encoding. One of three:

`Value` -- a result model.

`NeedsInput` -- the questions a handler needs answered, and the state it left.
Handlers raise `NeedInput`, which the dispatcher converts; they never build
`InputRequiredResult`, which is `2026-07-28`-specific and lives behind the
adapter.

`Failure` -- a kind from the normalized vocabulary, a message, and optional
data. The adapter maps the kind to its revision's code and status.

## `Exchange`

One request's worth of everything a handler might need. It arrives by
annotation.

| | |
| --- | --- |
| `ex.ask(key, request, default=...)` | Put a question, however this revision can |
| `ex.answered(key)`, `ex.accepted(key)`, `ex.action(key)` | What came back |
| `ex.log(level, data, logger=...)` | Report, if the client asked for that severity |
| `ex.progress(done, total, message=...)` | Report progress on a streaming call |
| `ex.session` | The session a handshake opened, or `None` |
| `ex.sessions` | Open or reach one by handle, on any revision |
| `ex.state` | What the previous attempt left |
| `ex.client_info` | Who is calling |
| `ex.request` | The aiohttp request, for what middleware decided |
| `ex.cancel()`, `ex.wait_cancelled()` | Cancellation |

It also resolves dependencies, and closes anything scoped to the request when
the request ends.

## `Dispatcher`

Routes an `Operation` to its handler with a `match` over every member, so a
renamed method is a type error rather than a silent "unknown method" at runtime.

It is also where the two things that wrap every call happen: the state a client
carried is restored before dispatch and the state a handler leaves is stored
after it, and a `NeedInput` that no client can answer becomes the failure the
specification names.

## `Endpoint`

The transport. It selects the adapter, decodes, decides whether the reply is one
JSON body or a stream, dispatches, and encodes.

A reply is streamed when the call has something to send before its result: a
tool declared `streaming=True`, a `subscriptions/listen` that runs until the
client goes away, or a question this revision would have to push.

`GET` is the notification stream for the revisions that read one. This endpoint
returns `405` for `DELETE`; its sessions end by expiration, and every request
that names a session renews it. A request naming a session the store does not
hold is answered `404`, so the client shakes hands again.

## Caching

The dispatcher builds each listing from the current registry, projects it for
the selected revision, sorts it, and applies the requested cursor. It does not
cache serialized lists. New registrations therefore appear on the next request.

On `2026-07-28`, cacheable results carry freshness hints. The default is
`ttlMs: 0` with `cacheScope: "private"`. Resource declarations can override
these values, and extension handlers can return `CacheableResult` subclasses.
These hints describe client caching; they do not cache server handler results.
