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

Apple M4, 10 cores, macOS 26.2. Python 3.10.11, `aiohttp` 3.14.3, `pydantic`
2.13.4, `mcp` 2.0.0, `uvicorn` 0.51.0. Loopback, no TLS. One call at a time,
`--calls 1200 --warmup 200 --repeats 3`; every row is measured three times in
rotation and keeps its best round.

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
| **aiohttp-tiny-mcp**       |     **3687** |     **3562** |     **3629** |    **3607** |   **3624** |
| official SDK, with session |         1078 |         1031 |         1033 |        1030 |       1393 |
| official SDK, stateless    |          889 |          896 |          904 |         868 |       1462 |
| aiohttp, no protocol       |         4779 |              |              |             |            |
| uvicorn, no protocol       |         3731 |              |              |             |            |

The same tool returning a plain string instead of a model:

| Server                     | 2024-11-05 | 2025-03-26 | 2025-06-18 | 2025-11-25 | 2026-07-28 |
|----------------------------|-----------:|-----------:|-----------:|-----------:|-----------:|
| **aiohttp-tiny-mcp**       |   **3659** |   **3736** |   **3730** |   **3781** |   **3656** |
| official SDK, with session |       1044 |       1025 |       1019 |       1023 |       1656 |
| official SDK, stateless    |        878 |        888 |        892 |        885 |       1668 |
| aiohttp, no protocol       |       4686 |            |            |            |            |
| uvicorn, no protocol       |       3705 |            |            |            |            |

**The revision costs this package nothing measurable.** Five revisions, and
the spread between them is smaller than the spread between two runs of the
same row. Serving `2024-11-05` and `2026-07-28` from one set of handlers is
not paid for at call time.

**What the SDK spends is largely the session.** Same server, same handler,
same stack; only the revision differs. The four revisions that carry a session
sit near 1000 calls a second. `2026-07-28`, the one with no session at all,
reaches 1400-1670.

**The SDK's stateless mode is the slower of its two.** `stateless_http=True`
does not remove the session -- it builds and destroys one per request -- and
on the legacy revisions it costs about 15% against keeping one.

At 32 calls in flight the gap widens rather than closes: this package reaches
5510-5913 against a floor of 8135, while the SDK's session revisions reach
1051-1159 against a floor of 5715, with a 99th percentile an order of
magnitude apart.

## Client over HTTP, per revision

The server is held constant down each block, so what varies is the cost of
building a request and reading a reply.

| Client                           | Against tiny-mcp  | Against SDK stateless  | Against SDK session  |
|----------------------------------|------------------:|-----------------------:|---------------------:|
| raw aiohttp, no client library   |              3692 |                    866 |                 1027 |
| **aiohttp-tiny-mcp, 2024-11-05** |          **3353** |                    975 |                 1188 |
| **aiohttp-tiny-mcp, 2025-03-26** |          **3395** |                    994 |                 1179 |
| **aiohttp-tiny-mcp, 2025-06-18** |          **3491** |                    983 |                 1183 |
| **aiohttp-tiny-mcp, 2025-11-25** |          **3480** |                   1009 |                 1164 |
| **aiohttp-tiny-mcp, 2026-07-28** |          **3345** |                   1418 |                 1404 |
| official SDK, 2025-11-25         |               783 |                    384 |                  399 |

Against the same server the two clients run at 3480 and 783 calls a second:
about a millisecond of client-side work per call. This package's client stays
within 5-9% of a hand-written `aiohttp` loop.

The official client appears once rather than once per revision. It offers only
`LATEST_PROTOCOL_VERSION` and negotiates down, and nothing in its API asks for
another; its row is labelled with what the handshake actually settled on.

## Over stdio

One subprocess, one pipe, no web framework on either side. Calls a second.

| Pairing                                   | `add`, a model  | `text`, a string  |
|-------------------------------------------|----------------:|------------------:|
| raw pipe, no protocol                     |           14338 |             14072 |
| **tiny-mcp 2024-11-05 client and server** |        **6797** |          **7163** |
| **tiny-mcp 2025-03-26 client and server** |        **6845** |          **7121** |
| **tiny-mcp 2025-06-18 client and server** |        **7031** |          **7165** |
| **tiny-mcp 2025-11-25 client and server** |        **6773** |          **7265** |
| **tiny-mcp 2026-07-28 client and server** |        **5813** |          **6415** |
| official SDK client -> tiny-mcp server    |            3350 |              3567 |
| tiny-mcp 2025-11-25 client -> SDK server  |            1870 |              1860 |
| official SDK client and server            |            1468 |              1453 |

This is the comparison with the fewest things in it, and the ratios are the
largest: against the same server the two clients are 6773 and 3350; against
the same client the two servers are 6773 and 1870.

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
  ceiling, runs 10-15% *below* both client libraries against servers that
  frame answers as event streams, and the cause is not isolated.
  `benchmarks/README.md` records that rather than hiding it.
