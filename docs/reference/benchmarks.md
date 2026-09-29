# What it costs

This page is the motivation for the library, stated in numbers rather than in
adjectives. It compares this package with the official Python SDK on one
`tools/call`, per protocol revision, for the server and for the client
separately, over HTTP and over a pipe.

The harness lives in `benchmarks/` in the source tree and is not installed
with the package. Reproduce any row with:

```bash
uv run python -m benchmarks.run --suite server
uv run python -m benchmarks.run --suite client
uv run python -m benchmarks.run --suite stdio
uv run python -m benchmarks.run --concurrency 32 --calls 3000
uv run python -m benchmarks.schema_cost       # one request, without a socket
```

## How to read it

Both libraries serve the same tool -- add two integers, return a model. What
differs is the protocol machinery between the socket and that function.

The official SDK row is its current high-level server (`MCPServer` in
`mcp==2.0.0`; older SDK releases called the comparable API `FastMCP`). This is
an implementation comparison, not a claim that the two projects expose the
same public class names. The benchmark uses the SDK version pinned in the
conditions below.

Over HTTP they do not stand on the same web framework: this package is
aiohttp, the official SDK is Starlette on uvicorn. So every HTTP table ends
with two rows that speak **no protocol at all** -- one hand-written handler per
stack, parsing the request and answering a fixed JSON body. Compare a row with
the floor of its own stack, not with the other library's. Without those rows a
reader compares two web servers and believes they compared two MCP libraries.

The stdio tables have no such problem, and are the cleanest comparison here:
one subprocess, one pipe, no framework on either side.

The server rows have no client library in them. The bytes of one real request
are recorded, then replayed with nothing but `aiohttp` in the way. The client
rows hold the server constant and vary the client, including the cross
pairings: this package's client against the SDK's servers, and the SDK's
client against this one.

## Conditions

Apple M4, 10 cores, macOS 26.2. Python 3.14.2, `aiohttp` 3.14.3, `pydantic`
2.13.5, `mcp` 2.0.0, `uvicorn` 0.53.0, `starlette` 1.6.0. Loopback, no TLS.
One call at a time, `--calls 1500 --warmup 200 --repeats 3`; every row is
measured three times in rotation and keeps its best round.

**Every logger is set to ERROR**, on both sides. This matters: starting the
SDK's server sets the root logger to INFO and installs a `rich` handler for
the whole process, after which its stateless server writes a line per request
and `httpx` writes another. Left on, that costs whoever talks to it 17-25% of
its rate. It is a real cost of a deployment that logs at INFO, and it is not
what these tables measure, so it is turned off for both alike.

## Server over HTTP, per revision

Calls a second.

| Server                     | 2024-11-05   | 2025-03-26   | 2025-06-18   |  2025-11-25 | 2026-07-28 |
|----------------------------|-------------:|-------------:|-------------:|------------:|-----------:|
| **aiohttp-tiny-mcp**       |     **5722** |     **5685** |     **5839** |    **5780** |   **5458** |
| official SDK, with session |         1263 |         1237 |         1277 |        1235 |       1616 |
| official SDK, stateless    |         1085 |         1105 |         1079 |        1095 |       1649 |
| aiohttp, no protocol       |         7208 |              |              |             |            |
| uvicorn, no protocol       |         4866 |              |              |             |            |

The same tool returning a plain string instead of a model:

| Server                     | 2024-11-05 | 2025-03-26 | 2025-06-18 | 2025-11-25 | 2026-07-28 |
|----------------------------|-----------:|-----------:|-----------:|-----------:|-----------:|
| **aiohttp-tiny-mcp**       |   **5621** |   **5624** |   **5680** |   **5680** |   **5428** |
| official SDK, with session |       1272 |       1288 |       1278 |       1239 |       1909 |
| official SDK, stateless    |       1059 |       1086 |       1062 |       1060 |       1888 |
| aiohttp, no protocol       |       7193 |            |            |            |            |
| uvicorn, no protocol       |       4809 |            |            |            |            |

**The revision costs this package nothing measurable.** Five revisions, and
the spread between them is smaller than the spread between two runs of the
same row. Serving `2024-11-05` and `2026-07-28` from one set of handlers is
not paid for at call time.

**What the SDK spends is largely the session.** Same server, same handler,
same stack; only the revision differs. The four revisions that carry a session
sit near 1100-1300 calls a second. `2026-07-28`, the one with no session at
all, reaches 1600-1900.

**The SDK's stateless mode is the slower of its two.** `stateless_http=True`
does not remove the session -- it builds and destroys one per request -- and
on the legacy revisions it costs about 13% against keeping one.

At 32 calls in flight the gap widens rather than closes: this package reaches
7673-8325 against a floor of 12654, while the SDK reaches 1402-1571 against a
floor of 7698. The tails separate further than the rates do: 4.8-6.4 ms at the
99th percentile here against 31-92 ms there.

Listing the catalogue behaves like calling it: `tools/list` runs at 5420-5637
against the same floors, or 76-80% of them. Revisions before `2026-07-28`
simplify a schema for the client, and that projection is derived once per
revision and kept on the tool, so a catalogue is not rebuilt per request.

`benchmarks/schema_cost.py` measures the same request with no transport under
it, which is where that shows. On `2025-11-25`, with a twelve-field argument
model carrying `$defs`, enums and nullable unions, one `tools/call` costs 19
us and one `tools/list` 31 us for a 3 KB answer. The two-field tool of the
tables above costs 18 us and 14 us. A larger schema therefore moves
`tools/list`, which writes it, and leaves `tools/call`, which does not, where
it was.

## Client over HTTP, per revision

The server is held constant down each block, so what varies is the cost of
building a request and reading a reply.

| Client                           | Against aiohttp-tiny-mcp  | Against SDK stateless  | Against SDK session  |
|----------------------------------|--------------------------:|-----------------------:|---------------------:|
| raw aiohttp, no client library   |              5632 |                   1034 |                 1237 |
| **aiohttp-tiny-mcp, 2024-11-05** |          **5112** |                   1135 |                 1326 |
| **aiohttp-tiny-mcp, 2025-03-26** |          **5169** |                   1130 |                 1318 |
| **aiohttp-tiny-mcp, 2025-06-18** |          **5225** |                   1125 |                 1339 |
| **aiohttp-tiny-mcp, 2025-11-25** |          **5247** |                   1124 |                 1293 |
| **aiohttp-tiny-mcp, 2026-07-28** |          **4888** |                   1658 |                 1656 |
| official SDK, 2025-11-25         |              1465 |                    613 |                  652 |

Against the same server the two clients run at 5247 and 1465 calls a second:
about half a millisecond of client-side work per call. This package's client
stays within 7-13% of a hand-written `aiohttp` loop.

The official client appears once rather than once per revision. It offers only
`LATEST_PROTOCOL_VERSION` and negotiates down, and nothing in its API asks for
another; its row is labelled with what the handshake actually settled on.

## Over stdio

One subprocess, one pipe, no web framework on either side. Calls a second.

| Pairing                                            | `add`, a model  | `text`, a string  |
|----------------------------------------------------|----------------:|------------------:|
| raw pipe, no protocol                              |           16330 |             15787 |
| **aiohttp-tiny-mcp 2024-11-05 client and server**  |        **8341** |          **8653** |
| **aiohttp-tiny-mcp 2025-03-26 client and server**  |        **8285** |          **8811** |
| **aiohttp-tiny-mcp 2025-06-18 client and server**  |        **8313** |          **9002** |
| **aiohttp-tiny-mcp 2025-11-25 client and server**  |        **8486** |          **8931** |
| **aiohttp-tiny-mcp 2026-07-28 client and server**  |        **7030** |          **7152** |
| official SDK client -> aiohttp-tiny-mcp server     |            3812 |              4094 |
| aiohttp-tiny-mcp 2025-11-25 client -> SDK server   |            2051 |              2070 |
| official SDK client and server                     |            1573 |              1581 |

This is the comparison with the fewest things in it, and the ratios are the
largest: against the same server the two clients are 8486 and 3812; against
the same client the two servers are 8486 and 2051.

## What these numbers are not

- **Not a claim about your machine.** One run, one laptop, loopback, no TLS,
  no proxy. Compare rows within a table, never a table with someone else's.
- **Not a claim about your handler.** Both tools here are trivial on purpose.
  Real work would swamp every difference on this page, which is the honest
  thing to say about it: if a tool call spends 50 ms in a database, none of
  this matters.
- **Not a feature comparison.** This package provides extensions, Skills,
  aiohttp middleware, and [authentication policies](../guide/auth.md). It has no built-in sampling
  or roots API. See
  [how this compares with the official SDK](../concepts.md#official-python-sdk)
  for what each one does and does not do.
- **Not a distributed-system benchmark.** Every server and driver runs on one
  machine over loopback. The benchmark does not exercise a shared
  `SessionStore`, a shared `Hub`, a load balancer, a second worker, a remote
  database, or a question that resumes on another request. Those are the
  conditions this project was designed for, but measuring them requires a
  deployment benchmark with real backend implementations and network
  topology. The numbers here describe the per-request protocol overhead before
  application work and distributed coordination are added.
- **Not a many-client benchmark.** The HTTP rows reuse one initialized client
  session per server while calls are concurrent. This measures contention in a
  hot session; it does not model a fleet of independent clients opening and
  refreshing sessions at the same time.
- **Best-round results are optimistic.** Each row is measured in several
  rotated rounds and the fastest round is kept. This reduces the effect of a
  busy laptop, but it also favors unusually quiet rounds. Treat the values as
  within-run comparisons, not confidence intervals or capacity guarantees.
- **Not a latency promise for remote-web-mcp.** TLS, proxy buffering, network
  RTT, backend contention, open-stream lifetimes, and failover dominate a
  remote deployment. Use these rows to compare the two implementations under
  identical local conditions, then measure your actual deployment separately.
- **Not free of loose ends.** Over HTTP the raw driver, which should be the
  ceiling, runs 4-10% *below* both client libraries against servers that
  frame answers as event streams, and the cause is not isolated.
  `benchmarks/README.md` records that rather than hiding it.
