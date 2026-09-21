# Choosing a revision

The server does not negotiate in the usual sense. It works out which revision a
message is written in, once per request, before decoding anything.

## Why it happens first

The envelope shape is itself revision-dependent -- `2025-03-26` permits JSON-RPC
batch arrays and the others do not -- so a typed decode cannot happen until the
revision is known. A shallow parse is the smallest thing that makes selection
total.

That shallow parse is the `Preamble`: the raw bytes, `json.loads` output or
`None`, the headers, and the three places a version can be stated. Building one
validates nothing beyond "is this JSON".

## The algorithm

Executed once per POST.

0. **Unparseable body** -- select the fallback adapter and let it render a parse
   error.
1. **`?mcp=` in the URL** -- resolve it and stop. See below.
2. **Header and body disagree** -- `-32020`, header mismatch.
3. **Body states a version** (`_meta`) -- select the adapter that claims it, or
   refuse with the supported list and what was requested.
4. **Header states a version** -- the same, applied to `MCP-Protocol-Version`.
5. **The method is `initialize`** -- select the stable revision, which reads what
   the client asked for and negotiates down inside its own handshake.
6. **Nothing states anything** -- `2025-03-26`, the revision that predates the
   header. Its absence is precisely what implies it.

Body metadata outranks the header. On `2026-07-28` the `_meta` value is the
source of truth and the header is its transport mirror, so the mirror never
overrides the original.

<!-- name: test_selection -->
```python
from aiohttp_tiny_mcp.protocol.core import Preamble
from aiohttp_tiny_mcp.protocol.selection import AdapterSet

adapters = AdapterSet.default()


def chosen(body: bytes, headers: dict | None = None, query: dict | None = None) -> str:
    return adapters.select(Preamble.of(body, headers or {}, query or {})).version


# Nothing stated at all.
assert chosen(b'{"jsonrpc":"2.0","id":1,"method":"tools/list"}') == "2025-03-26"

# The header alone.
assert (
    chosen(
        b'{"jsonrpc":"2.0","id":1,"method":"tools/list"}',
        {"MCP-Protocol-Version": "2025-11-25"},
    )
    == "2025-11-25"
)

# The body alone, which outranks a header that is absent.
assert (
    chosen(
        b'{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{"_meta":'
        b'{"io.modelcontextprotocol/protocolVersion":"2026-07-28"}}}'
    )
    == "2026-07-28"
)
```

## The `?mcp=` pin

A revision may be pinned on the endpoint URL:

```
POST /mcp?mcp=2025-11-25
```

This outranks everything, including body metadata, and a header/body
disagreement it resolves is not an error.

<!-- name: test_selection -->
```python
assert (
    chosen(
        b'{"jsonrpc":"2.0","id":1,"method":"tools/list"}',
        {"MCP-Protocol-Version": "2026-07-28"},
        {"mcp": "2025-06-18"},
    )
    == "2025-06-18"
)
```

The asymmetry is deliberate. The pin is written by whoever deployed the URL, not
by the client, so it is the one place an operator can say "this endpoint speaks
this revision" -- the fix for a client that negotiates badly, or that can only be
configured with a URL and no custom headers. Both kinds exist; see
[Client compatibility](parity.md#client-compatibility).

It chooses which revision's rules apply. It does not waive them: a request
pinned to `2026-07-28` must still satisfy that revision's HTTP binding.

## Refusal

An unsupported version is refused with the list of what is supported and what
was asked for, so a client can pick again rather than guess.

<!-- name: test_selection -->
```python
import pytest

from aiohttp_tiny_mcp.protocol.core import FailureKind, Rejected

with pytest.raises(Rejected) as raised:
    adapters.select(
        Preamble.of(
            b'{"jsonrpc":"2.0","id":1,"method":"tools/list"}',
            {"MCP-Protocol-Version": "1999-01-01"},
        )
    )

failure = raised.value.failure
assert failure.kind is FailureKind.UNSUPPORTED_VERSION
assert failure.data["requested"] == "1999-01-01"
assert "2026-07-28" in failure.data["supported"]
```

The refusal is rendered by the fallback adapter, which is the newest the server
speaks. Only the newer revisions define the structured error that lets a client
recover, and an older client ignores the extra fields harmlessly.

## Superseding

An adapter may claim revisions other than its own. The legacy trio project
identically in most respects, so one class answers for several dates where that
is true, and subclasses diverge only where they must.

<!-- name: test_selection -->
```python
assert set(adapters.versions) >= {"2026-07-28", "2025-11-25", "2025-06-18", "2025-03-26"}
```

## Remembering the choice

On a revision with a handshake the negotiated version is stored in the session,
so a later request that states nothing is still decoded by the revision it
negotiated rather than by step 6's assumption. The client is still required to
send the header, and this is a fallback rather than a replacement for it.

Over stdio there is no session and none is needed: one connection serves one
client, so the choice is kept on the connection.
