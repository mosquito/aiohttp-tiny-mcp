# What varies between revisions

The adapter surface is derived from this inventory rather than invented ahead
of it. Everything here is drawn from the published revisions and verified
against a real implementation where it was not obvious.

## The inventory

| Concern | 2026-07-28 | 2025-11-25 / 2025-06-18 | 2025-03-26 |
| --- | --- | --- | --- |
| Server description | `server/discover` | `initialize` + `notifications/initialized` | same |
| Session | none; state carried by explicit id | `Mcp-Session-Id` | same |
| `GET` stream | removed, answered `405` | standalone SSE stream | same |
| `MCP-Protocol-Version` header | required, must equal the `_meta` value | required after `initialize` | not defined; its absence implies this revision |
| `Mcp-Method` header | required on every request | not defined | not defined |
| `Mcp-Name` header | required for `tools/call`, `resources/read`, `prompts/get` | not defined | not defined |
| `Mcp-Param-*` headers | from `x-mcp-header` annotations, must mirror the body | not defined | not defined |
| Batching | one message per POST | one message per POST | JSON-RPC batch arrays |
| Unknown method | `404` + `-32601` | `200` + `-32601` | same |
| Header/body mismatch | `400` + `-32020` | not defined | not defined |
| Unsupported version | `400` + `-32022`, listing `supported` and `requested` | refused at `initialize` | refused at `initialize` |
| Missing client capability | `400` + `-32021`, listing `requiredCapabilities` | not defined | not defined |
| Result envelope | carries `resultType` | none | none |
| Ask the user | MRTR | pushed `elicitation/create` | no mechanism; see below |
| Cache hints | `ttlMs`, `cacheScope` on results | absent | absent |
| Long-lived notifications | `subscriptions/listen` response stream | standalone `GET` stream | same |
| `inputSchema` | full JSON Schema 2020-12 | restricted in practice; `$ref` poorly supported | same |
| `outputSchema` | unrestricted; `structuredContent` may be any JSON | object-rooted; `structuredContent` must be an object | neither exists |
| Display `title` on a tool, resource or prompt | yes | yes | no |
| `resource_link` content | yes | yes | no |
| Logging | per-request `_meta` key | `logging/setLevel`, kept for the session | same |
| Cancellation | close the response stream | `notifications/cancelled` | same |
| Extensions | declared in `server/discover`; custom methods callable | files and manifest through resources | same |

2024-11-05 (HTTP+SSE) is a different transport shape entirely -- two endpoints
and an `endpoint` event as the first message -- so it is implemented by the
opt-in `SseEndpoint`, rather than by the Streamable HTTP adapter. Its
handshake selects any revision that still has a handshake; `2026-07-28` is
refused because its required headers cannot travel with HTTP+SSE messages. See
[HTTP+SSE](../deployment/transports.md#httpsse-for-2024-11-05-clients).

## The three ways to ask

This is where the revisions differ most, and where the package earns its keep.
Each adapter declares which it has, and everything above the adapter branches on
the declaration rather than on a version string.

```{list-table}
:header-rows: 1

* - Flag
  - Meaning
  - Held by
* - `can_ask`
  - The question is the result; the client calls again with the answers.
  - 2026-07-28
* - `can_push_ask`
  - The question is pushed on the call's open stream and answered separately.
  - 2025-11-25, 2025-06-18
* - `asks_in_arguments`
  - The question rides in the tool call itself.
  - 2025-03-26, 2024-11-05
```

### MRTR, on 2026-07-28

The server answers with `resultType: "input_required"`, carrying
`inputRequests` and an opaque `requestState`. The client calls again with
`inputResponses` and that state.

The handler runs from the beginning on each attempt. `ask` returns immediately
for anything already answered, so the handler reads as if it had blocked.

The client sends every answer collected so far on each attempt, not only the
newest, because the handler starts over and would otherwise ask the same
question forever.

### A pushed request, on 2025-11-25 and 2025-06-18

The server sends `elicitation/create` as a genuine server-initiated JSON-RPC
request on the stream of the call in flight, and waits. The client answers with
a plain JSON-RPC response, POSTed on its own.

Nothing pins that answer to the worker that asked. It is published to the hub
under a topic named for the question; the waiting worker reads it there. The
position is taken before the question goes out, so an answer that arrives before
the first poll is still delivered.

### Asking where the protocol cannot

`2025-03-26` predates elicitation. It has nothing to push and no field to carry
a question back in.

What it does have is a tool call, and a caller that reads the result and calls
again. The first call returns an ordinary result -- not an error, because the
call did not fail, it is unfinished -- whose text names exactly what finishes
it, as JSON to copy. The answers come back as two ordinary arguments,
`mcpAnswers` and `mcpState`, taken out before the tool's own model sees them.

Only a handler that takes the `Exchange` can ask, so only those tools carry the
two extra properties. Every other tool keeps the schema it would have had, and
the convention costs the model nothing where it cannot be used.

The same result repeats the questions in `_meta`, under a key named for this
package since no revision specifies one. A model reads the text; a program reads
the meta. That is what lets the bundled client answer them like any other
question.

<!-- name: async test_convention; fixtures: serve -->
```python
from pydantic import BaseModel

from aiohttp_tiny_mcp import Client, Exchange, Registry, elicit
from aiohttp_tiny_mcp.protocol.selection import AdapterSet

registry = Registry("gates", "1.0")


class Nothing(BaseModel):
    pass


@registry.tool
async def gate(args: Nothing, ex: Exchange) -> str:
    """One confirmation."""
    return "went ahead" if (await ex.ask("go", elicit("Go ahead?"))).accepted else "stopped"


@registry.tool
async def plain(args: Nothing) -> str:
    """Asks nothing."""
    return "fine"


url = await serve(registry)
adapter = AdapterSet.default().by_version["2025-03-26"]
async with Client(url, adapter) as client:
    await client.initialize()
    tools = {tool.name: tool for tool in await client.list_tools()}

assert "mcpAnswers" in tools["gate"].input_schema["properties"]
assert "mcpAnswers" not in tools["plain"].input_schema["properties"]
```

## What the client declares

A server puts a question only to a client that said it can answer. What "said"
means differs too: a revision with a handshake declares its capabilities once
and the server remembers them; `2026-07-28` restates them on every request and
silence there means no, not "whatever you said last time".

The argument convention needs no declaration at all. It is not a protocol
feature a client opts into -- it is a tool call and a caller reading the result,
which every client already does.

## Degradation that is not a choice

Some projection happens because the older revision cannot hold what was
declared:

- A schema with a nullable union loses the null branch, because a legacy client
  that cannot read the union would hide the tool entirely.
- `x-mcp-header` annotations are stripped: a legacy client would not mirror
  them, and a server that then required the header would deadlock.
- A schema that cannot be simplified losslessly means the tool is not offered on
  that revision at all.

Each is one decision in one adapter, visible in one place, rather than a
condition in every handler.
