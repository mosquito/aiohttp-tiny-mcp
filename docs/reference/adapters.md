# Adapters

One class per revision, over a normalized core that names no revision at all.
This is the whole architecture; everything else follows from it.

## Layering

```{mermaid}
flowchart TB
    subgraph public["Public API -- revision-free"]
        Registry
        Specs["ToolSpec / ResourceSpec / PromptSpec"]
        Bound["Bound (function + args model + dependency plan)"]
    end

    subgraph neutral["Normalized core -- revision-free"]
        Call
        Operation
        Outcome
        Dispatcher
        Exchange
    end

    subgraph adapters["Adapters -- one per revision"]
        Adapter["Adapter (ABC)"]
        A26["Adapter2026_07_28"]
        A25c["Adapter2025_11_25"]
        A25b["Adapter2025_06_18"]
        A25a["Adapter2025_03_26"]
    end

    subgraph transport["Transport"]
        Endpoint
        AdapterSet
        Preamble
    end

    Endpoint --> AdapterSet
    AdapterSet --> Adapter
    Endpoint --> Preamble
    Adapter --> Call
    Dispatcher --> Registry
    Dispatcher --> Outcome
    Adapter --> Outcome
    Exchange --> Adapter
    Registry --> Specs
    Specs --> Bound
    Adapter -.-> A26 & A25c & A25b & A25a
```

Adapters translate protocol messages into normalized calls, and no adapter
imports `Endpoint`. Extension registration checks method names against the
built-in adapter maps to prevent collisions. Extension dispatch still uses
`Operation`, `Call`, and the same dependency resolver as built-in handlers.

## Design goals

1. **Parity is the point.** One registration serves every revision, and the same
   caller code reaches every server. Where revisions differ, the difference is
   absorbed by the adapter and never by the application.
2. **The public API is revision-free.** Application code never names a version,
   never checks one, and never writes two variants of a handler.
3. **A new revision is a new file.** One `Adapter` subclass and one entry in an
   `AdapterSet`. No edits to `Endpoint`, `Dispatcher` or `Registry`.
4. **Removing a revision is a deletion.**
5. **Revision behaviour is testable in isolation.** An adapter is a pure-ish
   object over normalized inputs, so conformance is tested per revision without
   an HTTP server.
6. **Degradation is explicit.** Where a declaration cannot be represented, the
   adapter decides -- visibly, in one place -- whether to downgrade it or hide
   it.

## What an adapter is

One instance per revision, shared across requests. It holds no per-request
state; everything per-request lives on the `Exchange`.

<!-- name: test_adapters -->
```python
from aiohttp_tiny_mcp.protocol.selection import AdapterSet

adapter = AdapterSet.default().by_version["2026-07-28"]
assert adapter.version == "2026-07-28"
```

The class name carries the revision date -- `Adapter2026_07_28` -- which breaks
the usual naming convention on purpose: the name is greppable against the
specification.

## Behavioural flags

Everything above the adapter branches on these, never on a version string.

<!-- name: test_adapters -->
```python
flags = {
    adapter.version: (
        adapter.can_ask,
        adapter.can_push_ask,
        adapter.asks_in_arguments,
        adapter.has_handshake,
        adapter.allows_batch,
    )
    for adapter in AdapterSet.default().adapters
}

assert flags == {
    #                  can_ask  push   arguments  handshake  batch
    "2026-07-28": (True, False, False, False, False),
    "2025-11-25": (False, True, False, True, False),
    "2025-06-18": (False, True, False, True, False),
    "2025-03-26": (False, False, True, True, True),
    "2024-11-05": (False, False, True, True, False),
}
```

`carries_state` is `can_ask or asks_in_arguments`. These revisions return an
opaque request-state handle for the client to send back. The handler's state
remains in the server's store; it is not sent to the client.

<!-- name: test_adapters -->
```python
carries = {a.version for a in AdapterSet.default().adapters if a.carries_state}
assert carries == {"2026-07-28", "2025-03-26", "2024-11-05"}
```

`supports_extensions` is true on `2026-07-28`. It enables extension method
dispatch and discovery; other adapters expose bundled resources under their
legacy URI prefixes. Decoding receives the registry to resolve extension
methods without storing application handlers on a shared adapter.

## The surface

**Inbound.** `decode(preamble)` turns one message into zero or more `Call`s, or
`DecodeFailure`s that preserve the id so the error can quote it. The shared
pipeline does the common work and calls per-revision hooks: `params_model`,
`check_message`, `check_params`, `client_info_for`, `answers_for`, `actions_for`,
`build_call`.

**Outbound.** `encode(call, registry, outcome)` renders a final response
identically for JSON, SSE and stdio. `encode_value`, `encode_input_required` and
`encode_failure` are the three shapes.

**Projection.** `describe_tool`, `describe_resource`, `describe_prompt` and
`capabilities` project a declaration down to what the revision can express, or
return `None` to hide it.

**Transport.** `check_http` enforces whatever headers the revision requires.

**Client role.** `client_headers`, `client_handshake_params`,
`client_decorate_params` and `client_input_requests` -- what a client speaking
this revision must send, and how to read a question out of a result.

## Failure mapping

`FailureKind` is the normalized vocabulary. Each adapter maps it to the JSON-RPC
code and HTTP status its revision defines, so the same internal failure is
reported the way each client expects.

<!-- name: test_adapters -->
```python
from aiohttp_tiny_mcp.protocol.core import Failure, FailureKind

modern = AdapterSet.default().by_version["2026-07-28"]
legacy = AdapterSet.default().by_version["2025-11-25"]

unknown = Failure(FailureKind.UNKNOWN_METHOD, "no such method")
assert modern.http_status(unknown) == 404
assert legacy.http_status(unknown) == 200
```

Two codes exist only on `2026-07-28` because only that revision defines them:
`-32021` for a missing client capability, `-32022` for an unsupported version.
A legacy adapter reports the same internal failure in its own vocabulary.

## Argument failures are results, not errors

A tool called with arguments that fail validation did not fail to be called. The
model asked for something and deserves to be told what happened, so
`INVALID_ARGUMENTS` is never encoded as a JSON-RPC error -- it becomes a tool
result with `isError` set. It is in `FailureKind` so the dispatcher can name it,
not so an adapter can render it.

## Adding a revision

1. Write `protocol/vYYYY_MM_DD.py` with one `Adapter` subclass. Subclass the
   nearest existing revision and override what differs -- the legacy trio is
   three classes deep for exactly this reason.
2. Add it to the `AdapterSet`.
3. Set the behavioural flags to the truth.
4. Test it in isolation: `decode`, `encode`, and the projections.

Nothing else changes. If something else has to change, the core was not neutral
enough, and that is the bug.
