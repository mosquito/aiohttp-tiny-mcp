# How this is verified

A package that claims parity across five revisions has to show it rather than
assert it. Four things do that, and each catches something the others cannot.

## The parametrized suite

One suite runs every adapter against one registry. The harness supplies the
requests; each adapter supplies its expectations.

- **Method map.** Every operation an adapter exposes round-trips:
  `operation_for(method_for(op)) is op`.
- **Failure map.** Every `FailureKind` produces the code and status its revision
  defines, and the mapping is total.
- **Projection.** A listing validates against that revision's constraints, and a
  hidden tool is absent from the listing *and* unreachable by call.
- **Header rules.** Required headers are enforced or ignored exactly as
  specified, and encoded values are decoded before comparison.
- **Selection.** Each adapter is reachable, and the choice is stable under
  irrelevant header noise.
- **Caching.** Two identical cacheable requests produce byte-identical results,
  and a registration between them changes the output.

Cross-adapter, the same registry called through every revision produces
semantically equivalent results, and batch input is accepted by exactly the
adapters that allow it.

## Our client against our server

Every capability is driven end to end through the bundled `Client`, over a real
socket, once per revision. This is what catches a claim that holds in a unit
test and fails on the wire -- reading a stream to its end before looking at it,
for instance, which deadlocks exactly one case and no others.

## Independent implementations

Our own client agreeing with our own server proves only that they agree. The
suite therefore also drives the server with the official `mcp` SDK's
`ClientSession`, and drives a real SDK server with our client.

Those checks are where several assumptions were corrected. The SDK opens the
`GET` notification stream only once a session id has been issued; it delivers a
parsed notification rather than an envelope; it sets the `elicitation`
capability only when a callback is actually configured. None of that is in the
specification, and all of it matters.

## Two stacks in four processes

Every check above runs inside one process, where a shared store is indistinguishable
from a Python dictionary. The claim that a request can land on any worker needs
more than that, so one suite starts four servers in four processes -- two of
this package and two of the official SDK -- over one SQLite file, and plugs it
into each stack through that stack's own extension points: `SessionStore` and
`Hub` here, `RequestStateSecurity(keys=...)`, `SubscriptionBus` and `EventStore`
there. It confirms by PID that each pair really is two processes, then begins
work on one node and finishes it on the other. A fifth SDK node shares the file
but not the key, to separate what the store carries from what the client does.

This is also what measures the comparison in
[deployment design](../pieces.md#why-store-and-hub-exist) rather than asserting it:
round-trip state and change events cross processes on both stacks, and a
session with a handshake crosses on this one only. It found the defect that
made the difference visible in the first place -- an SSE `data:` line with no
payload, which the SDK writes to prime a stream and this package's client read
as JSON.

## The documentation

Every python block in these pages is executed by
[markdown-pytest](https://pypi.org/project/markdown-pytest/) under the same
suite as everything else. A page that stops being true stops passing.
The copyable test setup is documented in [Testing a server](../guide/testing.md).

That is not only a guard against rot. Writing this documentation found three
real defects: a client that never returned the session id a server issued, a
client that could not read a JSON-RPC error carrying `id: null`, and a `listen`
that yielded one extra message on `2026-07-28` and nothing equivalent elsewhere.

## Running it

```bash
uv run pytest          # tests, documentation and README
uv run ruff check .
uv run ty check src/ examples/
uv run sphinx-build -b html docs docs/_build
```

## Not implemented

**Resumption of a response stream.** The `GET` notification stream resumes
with `Last-Event-ID`; see [Transports](../deployment/transports.md#resuming-a-stream).
The stream a `POST` opens for one request does not: it carries no ids, and a
client that loses one re-sends the request. The SDK replays those behind its
`EventStore`.

**Batching over stdio.** `2025-03-26` permits JSON-RPC batch arrays and the HTTP
transport accepts them. stdio does not.

**The `Tasks` and `MCP Apps` extensions.**
