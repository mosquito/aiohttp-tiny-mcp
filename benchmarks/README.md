# Benchmarks

Not part of the test suite; CI does not run them. Run them:

```bash
uv run python -m benchmarks.run                          # everything
uv run python -m benchmarks.run --suite server
uv run python -m benchmarks.run --suite client
uv run python -m benchmarks.run --suite stdio
uv run python -m benchmarks.run --concurrency 32 --calls 3000
uv run python -m benchmarks.run --operations call text list
uv run python -m benchmarks.schema_cost                  # one request, no socket
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

`--operations list` times `tools/list` as well, which is where a tool's schema
is written. `benchmarks/schema_cost.py` is a fourth measurement, outside the
matrix: it answers one request in this process, with no socket under it, for
a small and for a realistic schema.

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
| Python                  | 3.14.2                                                  |
| `aiohttp` / `pydantic`  | 3.14.3 / 2.13.5                                         |
| `mcp`                   | 2.0.0                                                   |
| `uvicorn` / `starlette` | 0.53.0 / 1.6.0                                          |
| Transport               | Streamable HTTP over loopback, no TLS                   |
| Settings                | `--calls 1500 --warmup 200 --repeats 3`                 |
| Logging                 | every logger at ERROR, on both sides                    |

## Results

### Server, one call at a time

`add(a, b)` -- an argument model in, a result model out.

| Server               | Revision   | calls/s   | p50   |   p99 | of floor  |
|----------------------|------------|----------:|------:|-----:|----------:|
| aiohttp-tiny-mcp     | 2024-11-05 |      5722 |  0.16 |  0.18 |   **79%** |
| aiohttp-tiny-mcp     | 2025-03-26 |      5685 |  0.16 |  0.19 |   **79%** |
| aiohttp-tiny-mcp     | 2025-06-18 |      5839 |  0.15 |  0.18 |   **81%** |
| aiohttp-tiny-mcp     | 2025-11-25 |      5780 |  0.15 |  0.18 |   **80%** |
| aiohttp-tiny-mcp     | 2026-07-28 |      5458 |  0.16 |  0.19 |   **76%** |
| SDK, stateless       | 2024-11-05 |      1085 |  0.83 |  1.16 |       22% |
| SDK, stateless       | 2025-03-26 |      1105 |  0.80 |  1.18 |       23% |
| SDK, stateless       | 2025-06-18 |      1079 |  0.82 |  1.26 |       22% |
| SDK, stateless       | 2025-11-25 |      1095 |  0.82 |  1.11 |       23% |
| SDK, stateless       | 2026-07-28 |      1649 |  0.58 |  0.68 |       34% |
| SDK, with session    | 2024-11-05 |      1263 |  0.70 |  0.95 |       26% |
| SDK, with session    | 2025-03-26 |      1237 |  0.70 |  1.44 |       25% |
| SDK, with session    | 2025-06-18 |      1277 |  0.70 |  1.30 |       26% |
| SDK, with session    | 2025-11-25 |      1235 |  0.70 |  1.30 |       25% |
| SDK, with session    | 2026-07-28 |      1616 |  0.58 |  0.63 |       33% |
| aiohttp, no protocol | --         |      7208 |  0.12 |  0.16 |      100% |
| uvicorn, no protocol | --         |      4866 |  0.19 |  0.21 |      100% |

`text()` -- no arguments, a plain string out.

| Server               | Revision   | calls/s   | p50   |   p99 | of floor  |
|----------------------|------------|----------:|------:|-----:|----------:|
| aiohttp-tiny-mcp     | 2024-11-05 |      5621 |  0.16 |  0.18 |   **78%** |
| aiohttp-tiny-mcp     | 2025-03-26 |      5624 |  0.16 |  0.18 |   **78%** |
| aiohttp-tiny-mcp     | 2025-06-18 |      5680 |  0.16 |  0.18 |   **79%** |
| aiohttp-tiny-mcp     | 2025-11-25 |      5680 |  0.16 |  0.18 |   **79%** |
| aiohttp-tiny-mcp     | 2026-07-28 |      5428 |  0.17 |  0.19 |   **75%** |
| SDK, stateless       | 2024-11-05 |      1059 |  0.84 |  1.52 |       22% |
| SDK, stateless       | 2025-03-26 |      1086 |  0.82 |  1.47 |       23% |
| SDK, stateless       | 2025-06-18 |      1062 |  0.83 |  1.52 |       22% |
| SDK, stateless       | 2025-11-25 |      1060 |  0.83 |  1.17 |       22% |
| SDK, stateless       | 2026-07-28 |      1888 |  0.51 |  0.54 |       39% |
| SDK, with session    | 2024-11-05 |      1272 |  0.70 |  1.39 |       26% |
| SDK, with session    | 2025-03-26 |      1288 |  0.69 |  1.36 |       27% |
| SDK, with session    | 2025-06-18 |      1278 |  0.70 |  1.41 |       27% |
| SDK, with session    | 2025-11-25 |      1239 |  0.69 |  1.34 |       26% |
| SDK, with session    | 2026-07-28 |      1909 |  0.50 |  0.56 |       40% |
| aiohttp, no protocol | --         |      7193 |  0.12 |  0.14 |      100% |
| uvicorn, no protocol | --         |      4809 |  0.19 |  0.22 |      100% |

`tools/list` -- the catalogue, which is where a tool's schema is written.

| Server               | Revision   | calls/s   | p50   |   p99 | of floor  |
|----------------------|------------|----------:|------:|-----:|----------:|
| aiohttp-tiny-mcp     | 2024-11-05 |      5637 |  0.16 |  0.18 |   **80%** |
| aiohttp-tiny-mcp     | 2025-03-26 |      5631 |  0.16 |  0.18 |   **79%** |
| aiohttp-tiny-mcp     | 2025-06-18 |      5616 |  0.16 |  0.18 |   **79%** |
| aiohttp-tiny-mcp     | 2025-11-25 |      5615 |  0.16 |  0.18 |   **79%** |
| aiohttp-tiny-mcp     | 2026-07-28 |      5420 |  0.17 |  0.19 |   **76%** |
| SDK, stateless       | 2024-11-05 |      1145 |  0.82 |  1.36 |       24% |
| SDK, stateless       | 2025-03-26 |      1171 |  0.75 |  1.41 |       24% |
| SDK, stateless       | 2025-06-18 |      1187 |  0.74 |  1.22 |       25% |
| SDK, stateless       | 2025-11-25 |      1181 |  0.74 |  1.00 |       24% |
| SDK, stateless       | 2026-07-28 |      2368 |  0.40 |  0.51 |       49% |
| SDK, with session    | 2024-11-05 |      1368 |  0.62 |  1.28 |       28% |
| SDK, with session    | 2025-03-26 |      1386 |  0.62 |  0.85 |       29% |
| SDK, with session    | 2025-06-18 |      1408 |  0.62 |  0.83 |       29% |
| SDK, with session    | 2025-11-25 |      1392 |  0.63 |  1.14 |       29% |
| SDK, with session    | 2026-07-28 |      2291 |  0.40 |  0.47 |       47% |
| aiohttp, no protocol | --         |      7088 |  0.12 |  0.14 |      100% |
| uvicorn, no protocol | --         |      4824 |  0.19 |  0.25 |      100% |

Three things are in these tables.

**The revision costs this package nothing.** Five revisions, 75-81% of the
floor, and the spread between them is smaller than the spread between two runs
of the same row. The adapters are not free, but they are below the noise.

**The session costs the SDK more than everything else it does.** Same server,
same handler, same stack; only the revision differs. The four revisions that
carry a session sit at 22-29% of the floor. `2026-07-28`, which has no
session, reaches 33-40% on a call and 47-49% on a listing. Removing the
session is worth more than halving the work.

**The SDK's stateless mode is the slower of its two.** `stateless_http=True`
does not remove the session -- it builds and destroys one per request -- and
on the legacy revisions it costs 12-16% against keeping one. The name suggests
the opposite.

Listing costs this package the same as calling, because a tool definition is
derived once per revision and kept on the tool rather than rebuilt per
request. The two-field tool here cannot show what that saves; see
[what a schema costs](#what-a-schema-costs).

### Server, 32 calls in flight

`add(a, b)`, `--concurrency 32 --calls 3000 --repeats 2`. Latency stops being
the reciprocal of the rate here; read the rate.

| Server               | Revision   | calls/s   | p50   |    p99 | of floor  |
|----------------------|------------|----------:|------:|------:|----------:|
| aiohttp-tiny-mcp     | 2024-11-05 |      8147 |  3.22 |   6.37 |   **64%** |
| aiohttp-tiny-mcp     | 2025-03-26 |      7982 |  3.25 |   4.82 |   **63%** |
| aiohttp-tiny-mcp     | 2025-06-18 |      8325 |  3.13 |   5.20 |   **66%** |
| aiohttp-tiny-mcp     | 2025-11-25 |      8299 |  3.13 |   5.44 |   **66%** |
| aiohttp-tiny-mcp     | 2026-07-28 |      7673 |  3.43 |   5.17 |   **61%** |
| SDK, stateless       | 2024-11-05 |      1571 | 18.48 |  57.16 |       20% |
| SDK, stateless       | 2025-03-26 |      1549 | 18.00 |  44.80 |       20% |
| SDK, stateless       | 2025-06-18 |      1506 | 17.78 |  55.54 |       20% |
| SDK, stateless       | 2025-11-25 |      1448 | 18.26 |  60.96 |       19% |
| SDK, stateless       | 2026-07-28 |      2354 | 11.82 |  36.81 |       31% |
| SDK, with session    | 2024-11-05 |      1422 | 21.83 |  31.25 |       18% |
| SDK, with session    | 2025-03-26 |      1413 | 21.25 |  75.51 |       18% |
| SDK, with session    | 2025-06-18 |      1406 | 21.95 |  31.75 |       18% |
| SDK, with session    | 2025-11-25 |      1402 | 21.47 |  92.26 |       18% |
| SDK, with session    | 2026-07-28 |      2259 | 12.05 |  37.90 |       29% |
| aiohttp, no protocol | --         |     12654 |  1.66 |   4.93 |      100% |
| uvicorn, no protocol | --         |      7698 |  3.53 |   5.96 |      100% |

Under load the gap widens rather than closes: 5-6x on the rate, and a tail one
order of magnitude apart -- 4.8-6.4 ms against 31-92 ms at the 99th
percentile. The SDK's session revisions reach 18-20% of their floor here
against 25-27% one call at a time.

### Client, one call at a time

`add(a, b)`. The server is the constant down each block, so what varies is the
cost of building a request and reading a reply.

| Client                          | Server            | calls/s   |    p50   |     p99  |
|---------------------------------|-------------------|----------:|---------:|---------:|
| raw aiohttp, no client          | aiohttp-tiny-mcp  |      5632 |     0.16 |     0.18 |
| aiohttp-tiny-mcp 2024-11-05     | aiohttp-tiny-mcp  |      5112 |     0.19 |     0.22 |
| aiohttp-tiny-mcp 2025-03-26     | aiohttp-tiny-mcp  |      5169 |     0.19 |     0.21 |
| aiohttp-tiny-mcp 2025-06-18     | aiohttp-tiny-mcp  |      5225 |     0.18 |     0.21 |
| aiohttp-tiny-mcp 2025-11-25     | aiohttp-tiny-mcp  |      5247 |     0.18 |     0.20 |
| aiohttp-tiny-mcp 2026-07-28     | aiohttp-tiny-mcp  |      4888 |     0.20 |     0.23 |
| **official SDK 2025-11-25** | **aiohttp-tiny-mcp** | **1465** | **0.65** | **0.76** |
| raw aiohttp, no client          | SDK stateless     |      1034 |     0.89 |     1.31 |
| aiohttp-tiny-mcp 2024-11-05     | SDK stateless     |      1135 |     0.83 |     0.99 |
| aiohttp-tiny-mcp 2025-03-26     | SDK stateless     |      1130 |     0.83 |     1.10 |
| aiohttp-tiny-mcp 2025-06-18     | SDK stateless     |      1125 |     0.83 |     0.95 |
| aiohttp-tiny-mcp 2025-11-25     | SDK stateless     |      1124 |     0.83 |     1.00 |
| aiohttp-tiny-mcp 2026-07-28     | SDK stateless     |      1658 |     0.59 |     0.68 |
| **official SDK 2025-11-25** | **SDK stateless** | **613** | **1.54** | **1.94** |
| raw aiohttp, no client          | SDK session       |      1237 |     0.71 |     1.39 |
| aiohttp-tiny-mcp 2024-11-05     | SDK session       |      1326 |     0.70 |     0.83 |
| aiohttp-tiny-mcp 2025-03-26     | SDK session       |      1318 |     0.71 |     0.87 |
| aiohttp-tiny-mcp 2025-06-18     | SDK session       |      1339 |     0.70 |     0.97 |
| aiohttp-tiny-mcp 2025-11-25     | SDK session       |      1293 |     0.71 |     1.09 |
| aiohttp-tiny-mcp 2026-07-28     | SDK session       |      1656 |     0.59 |     0.67 |
| **official SDK 2025-11-25** | **SDK session** | **652** | **1.44** | **1.84** |

Against one server, this package's client runs at 87-93% of a hand-written
`aiohttp` loop. Against the same server the two clients are 5247 and 1465
calls a second: **3.6x**, or about half a millisecond of client-side work per
call. On the SDK's own servers the two are 1124 and 613: **1.8x**.

The cross pairings work in both directions. This package's client speaks all
five revisions to both SDK servers, and the official client speaks to this
one -- it negotiates `2025-11-25` there, which is the newest revision it and
this server agree on through a handshake.

### Over stdio

`--calls 1500 --warmup 200 --repeats 3`. One subprocess, one pipe.

| Pairing                                          | `add`, a model  | `text`, a string   |
|--------------------------------------------------|----------------:|-------------------:|
| raw pipe, no protocol                            |           16330 |              15787 |
| aiohttp-tiny-mcp 2024-11-05 client and server    |        **8341** |           **8653** |
| aiohttp-tiny-mcp 2025-03-26 client and server    |        **8285** |           **8811** |
| aiohttp-tiny-mcp 2025-06-18 client and server    |        **8313** |           **9002** |
| aiohttp-tiny-mcp 2025-11-25 client and server    |        **8486** |           **8931** |
| aiohttp-tiny-mcp 2026-07-28 client and server    |        **7030** |           **7152** |
| official SDK client -> aiohttp-tiny-mcp server   |            3812 |               4094 |
| aiohttp-tiny-mcp 2024-11-05 client -> SDK server |            2051 |               2060 |
| aiohttp-tiny-mcp 2025-03-26 client -> SDK server |            2069 |               2062 |
| aiohttp-tiny-mcp 2025-06-18 client -> SDK server |            2067 |               2072 |
| aiohttp-tiny-mcp 2025-11-25 client -> SDK server |            2051 |               2070 |
| aiohttp-tiny-mcp 2026-07-28 client -> SDK server |            1926 |               1942 |
| official SDK client and server                   |            1573 |               1581 |

The comparison with the fewest things in it, and the largest ratios. Against
the same server the two clients are 8486 and 3812; against the same client the
two servers are 8486 and 2051. Nothing here is a web framework: it is a pipe,
a process boundary, and the protocol.

`2026-07-28` is the slowest revision for this package over stdio, by about
17%. That is the one place a revision shows up at all, and it is the newest
one: it carries the protocol version and client capabilities in `_meta` on
every request, where the others state them once in a handshake.

### What a schema costs

```
No HTTP. Answer sizes are the bytes the server would write.

small schema
2024-11-05 tools/call                  50344 ops/s     19.9 us    104 B
2024-11-05 tools/list                  77547 ops/s     12.9 us    303 B
2025-03-26 tools/call                  51114 ops/s     19.6 us    104 B
2025-03-26 tools/list                  75877 ops/s     13.2 us    303 B
2025-06-18 tools/call                  54161 ops/s     18.5 us    137 B
2025-06-18 tools/list                  73488 ops/s     13.6 us    489 B
2025-11-25 tools/call                  54950 ops/s     18.2 us    137 B
2025-11-25 tools/list                  73870 ops/s     13.5 us    489 B
2026-07-28 tools/call                  40226 ops/s     24.9 us    249 B
2026-07-28 tools/list                  50668 ops/s     19.7 us    634 B

fat schema
2024-11-05 tools/call                  46426 ops/s     21.5 us    175 B
2024-11-05 tools/list                  39437 ops/s     25.4 us   2071 B
2025-03-26 tools/call                  49138 ops/s     20.4 us    175 B
2025-03-26 tools/list                  39953 ops/s     25.0 us   2071 B
2025-06-18 tools/call                  53120 ops/s     18.8 us    259 B
2025-06-18 tools/list                  32654 ops/s     30.6 us   2999 B
2025-11-25 tools/call                  52693 ops/s     19.0 us    259 B
2025-11-25 tools/list                  32705 ops/s     30.6 us   2999 B
2026-07-28 tools/call                  38048 ops/s     26.3 us    371 B
2026-07-28 tools/list                  26639 ops/s     37.5 us   3069 B
```

`uv run python -m benchmarks.schema_cost`. No socket: one request is decoded,
dispatched, encoded and serialized in this process, so what is left is the
protocol and nothing else.

`small` is the tool the tables above use. `fat` is what a real catalogue
holds: a twelve-field argument model with two `$defs`, an enum, a `Literal`
and nullable unions.

The schema moves `tools/list`, which writes it, and leaves `tools/call`, which
does not. On `2025-11-25` a listing goes from 14 us for 489 bytes to 31 us for
3 KB, while a call stays at 18-19 us either way. Revisions before `2026-07-28`
simplify a schema for the client -- inlining `$defs`, collapsing nullable
unions, stripping `x-mcp-header` -- and that projection is built once per
revision and kept on the tool. Were it rebuilt per request, the fat rows would
be several times slower and `tools/call` would pay it too.

`2026-07-28` is the slowest here on a listing because it writes the schema
whole, refs and all: 3069 bytes against 2071 on `2024-11-05`, which inlines
and simplifies.

### What per-request logging costs

Every logger is set to ERROR, which is a condition of the run rather than an
option. Worth recording what that suppresses.

Starting the SDK's server sets the **root** logger to INFO and installs a
`rich` handler on it, for the whole process. Importing `mcp` alone does not do
this; starting the server does. From then on its stateless server writes a
line for every session it opens and closes -- which is one per request -- and
`httpx` writes another for every call the official client makes.

Measured on `client: call` when this condition was found, on an earlier
interpreter. The rates are not the ones in the tables above; read the last
column:

| Row | logging off | as the SDK leaves it | |
| --- | ---: | ---: | ---: |
| aiohttp-tiny-mcp 2025-11-25 -> aiohttp-tiny-mcp server | 3455 | 3459 | no change |
| official SDK -> aiohttp-tiny-mcp server | 782 | 771 | -1% |
| raw aiohttp -> SDK stateless | 839 | 695 | **-17%** |
| aiohttp-tiny-mcp 2025-11-25 -> SDK stateless | 995 | 743 | **-25%** |
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
  frames its answers as an event stream it runs 4-10% *below* the client
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
