# Benchmarks

Not part of the test suite and not committed yet. Run them:

```bash
uv run python -m benchmarks.run                          # everything
uv run python -m benchmarks.run --suite server
uv run python -m benchmarks.run --suite stdio
uv run python -m benchmarks.run --concurrency 32 --calls 3000
```

## What is measured

Three matrices, not two pairings.

**Server: every server, on every revision.** Three servers are timed -- this
package, the official SDK with `stateless_http=True`, and the same SDK keeping
a session -- each on all five revisions. No client library is in the way: the
bytes of a real request are recorded once, by letting this package's own
client perform the call, and the loop then posts those bytes with nothing but
`aiohttp` in between. Recording rather than hand-writing the envelope means a
revision's shape cannot be got wrong.

**Client: every client, on every revision, against every server.** The cross
pairings are the point -- this package's client drives the SDK's servers and
the SDK's client drives this one. Holding the server constant down a column is
what isolates the cost of a client.

The official client appears once per server rather than once per revision. It
offers only `LATEST_PROTOCOL_VERSION` and negotiates down; nothing in its API
asks for another, and patching that constant changes nothing because the
server picks. Its row is labelled with the revision it actually settled on,
read back from the handshake.

**stdio: every pairing over a pipe.** No web framework on either side, which
makes it the cleanest comparison here. Over a pipe there is no server to drive
on its own -- a stdio server is one subprocess speaking to one client -- so
the rows are pairings, which is also how stdio is deployed. The floor is a
subprocess that reads a line and writes a fixed one.

Two tools are offered, and both are timed:

| Tool | Returns | What it isolates |
| --- | --- | --- |
| `add(a, b)` | a pydantic model | the usual path: output schema, structured content |
| `text()` | a plain string | the same path without a result model |

## The floor

Every table names two rows that are not MCP at all: one aiohttp handler and
one Starlette handler, each parsing the request and answering a fixed JSON
body. They exist because this package runs on aiohttp and the official SDK
runs on uvicorn, and **without them a reader compares two web servers and
believes they compared two MCP libraries.**

`of floor` is each row's rate as a share of its own stack answering that fixed
body. It is the number to read. A library that reaches 70% of its floor is
spending 30% of the request on protocol work; the absolute rates also carry
whatever separates aiohttp from uvicorn on the day.

## Conditions

Every number below came from one run on one machine. They compare rows within
that run and say nothing about any other machine.

|                         |                                                         |
|-------------------------|---------------------------------------------------------|
| Machine                 | Apple M4, 10 cores, macOS 26.2                          |
| Python                  | 3.10.11                                                 |
| `aiohttp` / `pydantic`  | 3.14.3 / 2.13.4                                         |
| `mcp`                   | 2.0.0                                                   |
| `uvicorn` / `starlette` | 0.51.0 / 1.3.1                                          |
| Transport               | Streamable HTTP over loopback, no TLS                   |
| Settings                | `--calls 1200 --warmup 200 --repeats 3`                 |
| Logging                 | every logger at ERROR, on both sides                    |

## Results

### Server, one call at a time

`add(a, b)` -- an argument model in, a result model out.

| Server               | Revision   | calls/s   | p50   | p99  | of floor  |
|----------------------|------------|----------:|------:|-----:|----------:|
| tiny-mcp             | 2024-11-05 |      3687 |  0.25 | 0.29 |   **77%** |
| tiny-mcp             | 2025-03-26 |      3562 |  0.26 | 0.29 |   **75%** |
| tiny-mcp             | 2025-06-18 |      3629 |  0.25 | 0.28 |   **76%** |
| tiny-mcp             | 2025-11-25 |      3607 |  0.25 | 0.29 |   **75%** |
| tiny-mcp             | 2026-07-28 |      3624 |  0.25 | 0.28 |   **76%** |
| SDK, stateless       | 2024-11-05 |       889 |  1.09 | 1.35 |       24% |
| SDK, stateless       | 2025-03-26 |       896 |  1.08 | 1.38 |       24% |
| SDK, stateless       | 2025-06-18 |       904 |  1.03 | 1.35 |       24% |
| SDK, stateless       | 2025-11-25 |       868 |  1.07 | 1.47 |       23% |
| SDK, stateless       | 2026-07-28 |      1462 |  0.66 | 0.80 |       39% |
| SDK, with session    | 2024-11-05 |      1078 |  0.83 | 1.27 |       29% |
| SDK, with session    | 2025-03-26 |      1031 |  0.93 | 1.10 |       28% |
| SDK, with session    | 2025-06-18 |      1033 |  0.85 | 1.15 |       28% |
| SDK, with session    | 2025-11-25 |      1030 |  0.92 | 1.21 |       28% |
| SDK, with session    | 2026-07-28 |      1393 |  0.66 | 0.85 |       37% |
| aiohttp, no protocol | --         |      4779 |  0.19 | 0.22 |      100% |
| uvicorn, no protocol | --         |      3731 |  0.25 | 0.28 |      100% |

`text()` -- no arguments, a plain string out.

| Server               | Revision   | calls/s   | p50   | p99   | of floor   |
|----------------------|------------|----------:|------:|------:|-----------:|
| tiny-mcp             | 2024-11-05 |      3659 |  0.25 |  0.29 |    **78%** |
| tiny-mcp             | 2025-03-26 |      3736 |  0.24 |  0.31 |    **80%** |
| tiny-mcp             | 2025-06-18 |      3730 |  0.24 |  0.31 |    **80%** |
| tiny-mcp             | 2025-11-25 |      3781 |  0.24 |  0.27 |    **81%** |
| tiny-mcp             | 2026-07-28 |      3656 |  0.25 |  0.28 |    **78%** |
| SDK, stateless       | 2024-11-05 |       878 |  1.10 |  1.37 |        24% |
| SDK, stateless       | 2025-03-26 |       888 |  1.09 |  1.38 |        24% |
| SDK, stateless       | 2025-06-18 |       892 |  1.08 |  1.44 |        24% |
| SDK, stateless       | 2025-11-25 |       885 |  1.04 |  1.36 |        24% |
| SDK, stateless       | 2026-07-28 |      1668 |  0.57 |  0.77 |        45% |
| SDK, with session    | 2024-11-05 |      1044 |  0.93 |  1.16 |        28% |
| SDK, with session    | 2025-03-26 |      1025 |  0.94 |  1.28 |        28% |
| SDK, with session    | 2025-06-18 |      1019 |  0.94 |  1.33 |        28% |
| SDK, with session    | 2025-11-25 |      1023 |  0.93 |  1.28 |        28% |
| SDK, with session    | 2026-07-28 |      1656 |  0.58 |  0.68 |        45% |
| aiohttp, no protocol | --         |      4686 |  0.19 |  0.22 |       100% |
| uvicorn, no protocol | --         |      3705 |  0.25 |  0.27 |       100% |

Three things are in these two tables.

**The revision costs this package nothing.** Five revisions, 75-77% of the
floor, and the spread between them is smaller than the spread between two runs
of the same row. The adapters are not free, but they are below the noise.

**The session costs the SDK more than everything else it does.** Same server,
same handler, same stack; only the revision differs. The four revisions that
carry a session sit at 23-29% of the floor. `2026-07-28`, which has no
session, reaches 39% with a result model and 45% without. Removing the session
is worth more than halving the work.

**The SDK's stateless mode is the slower of its two.** `stateless_http=True`
does not remove the session -- it builds and destroys one per request -- and
on the legacy revisions it costs 15% against keeping one. The name suggests
the opposite.

The result model is worth 3-5 points of floor to this package (75-77% becomes
78-81%) and nothing at all to the SDK on the revisions where the session
dominates.

### Server, 32 calls in flight

`add(a, b)`, `--concurrency 32 --calls 3000 --repeats 2`. Latency stops being
the reciprocal of the rate here; read the rate.

| Server               | Revision   | calls/s   | p50   |   p99  | of floor  |
|----------------------|------------|----------:|------:|-------:|----------:|
| tiny-mcp             | 2024-11-05 |      5510 |  4.47 |  20.07 |   **68%** |
| tiny-mcp             | 2025-03-26 |      5866 |  4.43 |   7.65 |   **72%** |
| tiny-mcp             | 2025-06-18 |      5902 |  4.24 |  21.04 |   **73%** |
| tiny-mcp             | 2025-11-25 |      5913 |  4.27 |  31.55 |   **73%** |
| tiny-mcp             | 2026-07-28 |      5703 |  4.33 |  24.06 |   **70%** |
| SDK, stateless       | 2024-11-05 |       978 | 25.17 |  73.74 |       17% |
| SDK, stateless       | 2025-03-26 |       792 | 26.35 | 147.41 |       14% |
| SDK, stateless       | 2025-06-18 |       791 | 26.19 | 102.23 |       14% |
| SDK, stateless       | 2025-11-25 |       736 | 27.48 | 138.45 |       13% |
| SDK, stateless       | 2026-07-28 |      1468 | 15.47 |  84.63 |       26% |
| SDK, with session    | 2024-11-05 |      1159 | 25.16 |  84.41 |       20% |
| SDK, with session    | 2025-03-26 |      1132 | 24.96 | 120.89 |       20% |
| SDK, with session    | 2025-06-18 |      1084 | 25.24 | 144.97 |       19% |
| SDK, with session    | 2025-11-25 |      1051 | 25.23 | 131.31 |       18% |
| SDK, with session    | 2026-07-28 |      1376 | 15.32 | 125.82 |       24% |
| aiohttp, no protocol | --         |      8135 |  2.39 |  76.08 |      100% |
| uvicorn, no protocol | --         |      5715 |  4.32 |   9.89 |      100% |

Under load the gap widens rather than closes: 6-8x on the rate, and a tail
that is one order of magnitude apart. The SDK's session revisions reach
13-20% of their floor here against 23-29% one call at a time.

### Client, one call at a time

`add(a, b)`. The server is the constant down each block, so what varies is the
cost of building a request and reading a reply.

| Client                      | Server            | calls/s   |    p50   |     p99  |
|-----------------------------|-------------------|----------:|---------:|---------:|
| raw aiohttp, no client      | tiny-mcp          |      3692 |     0.24 |     0.30 |
| tiny-mcp 2024-11-05         | tiny-mcp          |      3353 |     0.28 |     0.38 |
| tiny-mcp 2025-03-26         | tiny-mcp          |      3395 |     0.28 |     0.35 |
| tiny-mcp 2025-06-18         | tiny-mcp          |      3491 |     0.27 |     0.37 |
| tiny-mcp 2025-11-25         | tiny-mcp          |      3480 |     0.27 |     0.35 |
| tiny-mcp 2026-07-28         | tiny-mcp          |      3345 |     0.29 |     0.33 |
| **official SDK 2025-11-25** | **tiny-mcp**      |   **783** | **1.23** | **1.58** |
| raw aiohttp, no client      | SDK stateless     |       866 |     1.11 |     1.42 |
| tiny-mcp 2024-11-05         | SDK stateless     |       975 |     0.98 |     1.24 |
| tiny-mcp 2025-03-26         | SDK stateless     |       994 |     0.97 |     1.16 |
| tiny-mcp 2025-06-18         | SDK stateless     |       983 |     0.96 |     1.12 |
| tiny-mcp 2025-11-25         | SDK stateless     |      1009 |     0.96 |     1.12 |
| tiny-mcp 2026-07-28         | SDK stateless     |      1418 |     0.69 |     0.82 |
| **official SDK 2025-11-25** | **SDK stateless** |   **384** | **2.44** | **2.94** |
| raw aiohttp, no client      | SDK session       |      1027 |     0.85 |     1.38 |
| tiny-mcp 2024-11-05         | SDK session       |      1188 |     0.81 |     0.89 |
| tiny-mcp 2025-03-26         | SDK session       |      1179 |     0.82 |     0.99 |
| tiny-mcp 2025-06-18         | SDK session       |      1183 |     0.82 |     0.90 |
| tiny-mcp 2025-11-25         | SDK session       |      1164 |     0.82 |     0.89 |
| tiny-mcp 2026-07-28         | SDK session       |      1404 |     0.68 |     0.80 |
| **official SDK 2025-11-25** | **SDK session**   |   **399** | **2.31** | **3.17** |

Against one server, this package's client runs at 91-95% of a hand-written
`aiohttp` loop, and the official client at 21%. Against the same tiny-mcp
server the two clients are 3480 and 783 calls a second: **4.4x**, or about a
millisecond of client-side work per call.

The cross pairings work in both directions. This package's client speaks all
five revisions to both SDK servers, and the official client speaks to this
one -- it negotiates `2025-11-25` there, which is the newest revision it and
this server agree on through a handshake.

### Over stdio

`--calls 1500 --warmup 200 --repeats 3`. One subprocess, one pipe.

| Pairing                                  | `add`, a model  | `text`, a string   |
|------------------------------------------|----------------:|-------------------:|
| raw pipe, no protocol                    |           14338 |              14072 |
| tiny-mcp 2024-11-05 client and server    |        **6797** |           **7163** |
| tiny-mcp 2025-03-26 client and server    |        **6845** |           **7121** |
| tiny-mcp 2025-06-18 client and server    |        **7031** |           **7165** |
| tiny-mcp 2025-11-25 client and server    |        **6773** |           **7265** |
| tiny-mcp 2026-07-28 client and server    |        **5813** |           **6415** |
| official SDK client -> tiny-mcp server   |            3350 |               3567 |
| tiny-mcp 2024-11-05 client -> SDK server |            1863 |               1850 |
| tiny-mcp 2025-03-26 client -> SDK server |            1837 |               1848 |
| tiny-mcp 2025-06-18 client -> SDK server |            1867 |               1857 |
| tiny-mcp 2025-11-25 client -> SDK server |            1870 |               1860 |
| tiny-mcp 2026-07-28 client -> SDK server |            1729 |               1707 |
| official SDK client and server           |            1468 |               1453 |

The comparison with the fewest things in it, and the largest ratios. Against
the same server the two clients are 6773 and 3350; against the same client the
two servers are 6773 and 1870. Nothing here is a web framework: it is a pipe,
a process boundary, and the protocol.

`2026-07-28` is the slowest revision for this package over stdio, by about
15%. That is the one place a revision shows up at all, and it is the newest
one: it carries the protocol version and client capabilities in `_meta` on
every request, where the others state them once in a handshake.

### What per-request logging costs

Every logger is set to ERROR, which is a condition of the run rather than an
option. Worth recording what that suppresses.

Starting the SDK's server sets the **root** logger to INFO and installs a
`rich` handler on it, for the whole process. Importing `mcp` alone does not do
this; starting the server does. From then on its stateless server writes a
line for every session it opens and closes -- which is one per request -- and
`httpx` writes another for every call the official client makes.

Measured on `client: call`, before this condition was fixed:

| Row | logging off | as the SDK leaves it | |
| --- | ---: | ---: | ---: |
| tiny-mcp 2025-11-25 -> tiny-mcp server | 3455 | 3459 | no change |
| official SDK -> tiny-mcp server | 782 | 771 | -1% |
| raw aiohttp -> SDK stateless | 839 | 695 | **-17%** |
| tiny-mcp 2025-11-25 -> SDK stateless | 995 | 743 | **-25%** |
| official SDK -> SDK stateless | 385 | 346 | -10% |

The penalty lands on whoever talks to the SDK's stateless server. Its
session-keeping mode does not log per request. This package logs nothing per
request above DEBUG, and `aiohttp`'s client logs nothing at all, so its rows
do not move.

That cost is real for a deployment that logs at INFO. It is excluded here
because it is not what these tables are trying to measure, and because
excluding it treats both sides alike.

## What is not measured, and one thing that does not add up

- **The raw driver is a reference, not a ceiling.** Against a server that
  frames its answers as an event stream it runs 10-15% *below* the client
  libraries, consistently, and the cause has not been isolated: the requests
  are byte-identical and both read the stream the same way. Read the raw row
  as a rough floor for what the transport costs, and compare clients with each
  other rather than with it.
- **Loopback only.** No network, no TLS, no proxy. Every number is dominated
  by round trips on one machine, so treat them as relative.
- **One process.** Both servers share the machine with the driver, so this
  compares rows within one run, never runs across machines.
- **Trivial handlers.** Real work would swamp the differences. That is the
  point: what is left is the protocol machinery.
- **`2026-07-28` negotiates nothing**, having no handshake, so a server that
  quietly serves something else there cannot be marked. On the other four a
  row is marked `(spoke ...)` where the server refused the revision asked for.

## Reading a run

`calls/s` at `--concurrency 1` is really a latency measurement: one request at
a time, so the rate is the reciprocal of the round trip. Raise the concurrency
to ask about throughput instead. Both are worth having, and they do not rank
the same way.

Percentiles are milliseconds. Warm-up runs before the clock starts, because
the first calls of any client pay for a connection and a cold cache, and
reporting those beside the steady state describes neither.
