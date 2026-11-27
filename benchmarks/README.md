# Benchmarks

Not part of the test suite; CI does not run them. Run them:

```bash
uv run python -m benchmarks.run                          # everything
uv run python -m benchmarks.run --suite server
uv run python -m benchmarks.run --suite client
uv run python -m benchmarks.run --suite stdio
uv run python -m benchmarks.run --suite server --operations call --concurrency 32 --calls 3000 --repeats 2
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

The HTTP server tables include bare aiohttp and Starlette handlers that return
a fixed JSON body. `Of floor` divides each server's rate by its own stack's
baseline. The stdio tables include a raw pipe baseline. The HTTP client tables
contain MCP pairings only.

## Conditions

Measured on an Arch Linux x86-64 VM with 16 vCPUs on an AMD Ryzen AI 9 HX PRO
370, kernel `7.2.11-arch1-1`. CPython 3.14.2, aiohttp 3.14.5, Pydantic 2.14.4,
MCP SDK 2.2.0, uvicorn 0.54.0, and Starlette 1.10.0. Both HTTP listeners use
`TCP_NODELAY`. The driver and servers run on the same machine over loopback,
without TLS. Logging is set to ERROR and HTTP access logs are disabled.

The serial runs use 1,500 measured calls, 200 warmup calls, and three rotated
rounds per row. The concurrent run uses 3,000 calls, 200 warmup calls, two
rounds, and up to 32 calls in flight. Each table reports the fastest round
per row and the latency percentiles from that round.

## Results

### Server, add (model result)

| Pairing | Calls/s | Mean (ms) | p50 (ms) | p95 (ms) | p99 (ms) | Of floor |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| aiohttp-tiny-mcp 2024-11-05 | 5745 | 0.17 | 0.16 | 0.21 | 0.32 | 55% |
| aiohttp-tiny-mcp 2025-03-26 | 6001 | 0.16 | 0.15 | 0.20 | 0.32 | 58% |
| aiohttp-tiny-mcp 2025-06-18 | 6037 | 0.16 | 0.15 | 0.23 | 0.32 | 58% |
| aiohttp-tiny-mcp 2025-11-25 | 6248 | 0.16 | 0.15 | 0.18 | 0.26 | 60% |
| aiohttp-tiny-mcp 2026-07-28 | 5956 | 0.16 | 0.15 | 0.21 | 0.39 | 57% |
| SDK stateless 2024-11-05 | 802 | 1.20 | 1.05 | 1.59 | 2.55 | 13% |
| SDK stateless 2025-03-26 | 776 | 1.24 | 1.05 | 1.70 | 2.90 | 12% |
| SDK stateless 2025-06-18 | 726 | 1.33 | 1.08 | 1.42 | 3.11 | 11% |
| SDK stateless 2025-11-25 | 752 | 1.28 | 1.06 | 1.63 | 2.43 | 12% |
| SDK stateless 2026-07-28 | 1347 | 0.73 | 0.67 | 0.88 | 2.15 | 21% |
| SDK session 2024-11-05 | 852 | 1.13 | 0.93 | 1.21 | 2.58 | 13% |
| SDK session 2025-03-26 | 824 | 1.17 | 0.92 | 1.60 | 2.81 | 13% |
| SDK session 2025-06-18 | 846 | 1.14 | 0.90 | 1.28 | 2.32 | 13% |
| SDK session 2025-11-25 | 853 | 1.13 | 0.90 | 1.28 | 2.64 | 13% |
| SDK session 2026-07-28 | 1404 | 0.71 | 0.66 | 0.90 | 1.63 | 22% |
| aiohttp, no protocol | 10411 | 0.09 | 0.08 | 0.10 | 0.14 | 100% |
| uvicorn, no protocol | 6329 | 0.15 | 0.14 | 0.18 | 0.31 | 100% |

### Server, text (string result)

| Pairing | Calls/s | Mean (ms) | p50 (ms) | p95 (ms) | p99 (ms) | Of floor |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| aiohttp-tiny-mcp 2024-11-05 | 6263 | 0.15 | 0.15 | 0.18 | 0.22 | 64% |
| aiohttp-tiny-mcp 2025-03-26 | 5610 | 0.17 | 0.14 | 0.32 | 0.43 | 57% |
| aiohttp-tiny-mcp 2025-06-18 | 6744 | 0.14 | 0.14 | 0.16 | 0.20 | 69% |
| aiohttp-tiny-mcp 2025-11-25 | 6785 | 0.14 | 0.14 | 0.16 | 0.19 | 69% |
| aiohttp-tiny-mcp 2026-07-28 | 6148 | 0.16 | 0.14 | 0.19 | 0.30 | 63% |
| SDK stateless 2024-11-05 | 740 | 1.28 | 1.03 | 2.34 | 4.00 | 12% |
| SDK stateless 2025-03-26 | 777 | 1.24 | 1.02 | 1.53 | 2.89 | 12% |
| SDK stateless 2025-06-18 | 753 | 1.28 | 1.02 | 1.54 | 2.45 | 12% |
| SDK stateless 2025-11-25 | 775 | 1.24 | 1.01 | 1.59 | 3.05 | 12% |
| SDK stateless 2026-07-28 | 1667 | 0.59 | 0.54 | 0.77 | 1.61 | 26% |
| SDK session 2024-11-05 | 812 | 1.18 | 0.90 | 2.01 | 2.77 | 13% |
| SDK session 2025-03-26 | 873 | 1.09 | 0.90 | 1.24 | 1.74 | 14% |
| SDK session 2025-06-18 | 859 | 1.11 | 0.90 | 1.42 | 2.18 | 14% |
| SDK session 2025-11-25 | 866 | 1.11 | 0.88 | 1.10 | 2.77 | 14% |
| SDK session 2026-07-28 | 1711 | 0.58 | 0.54 | 0.64 | 1.95 | 27% |
| aiohttp, no protocol | 9821 | 0.10 | 0.08 | 0.12 | 0.30 | 100% |
| uvicorn, no protocol | 6310 | 0.15 | 0.14 | 0.18 | 0.30 | 100% |

### Server, tools/list

| Pairing | Calls/s | Mean (ms) | p50 (ms) | p95 (ms) | p99 (ms) | Of floor |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| aiohttp-tiny-mcp 2024-11-05 | 6379 | 0.15 | 0.14 | 0.17 | 0.30 | 60% |
| aiohttp-tiny-mcp 2025-03-26 | 6568 | 0.15 | 0.14 | 0.16 | 0.23 | 62% |
| aiohttp-tiny-mcp 2025-06-18 | 6307 | 0.15 | 0.14 | 0.17 | 0.27 | 59% |
| aiohttp-tiny-mcp 2025-11-25 | 6406 | 0.15 | 0.14 | 0.18 | 0.27 | 60% |
| aiohttp-tiny-mcp 2026-07-28 | 6028 | 0.16 | 0.15 | 0.19 | 0.32 | 57% |
| SDK stateless 2024-11-05 | 892 | 1.08 | 0.93 | 1.48 | 2.65 | 15% |
| SDK stateless 2025-03-26 | 862 | 1.12 | 0.93 | 1.22 | 2.31 | 14% |
| SDK stateless 2025-06-18 | 830 | 1.16 | 0.92 | 1.39 | 2.08 | 14% |
| SDK stateless 2025-11-25 | 823 | 1.17 | 0.93 | 1.49 | 2.62 | 13% |
| SDK stateless 2026-07-28 | 2155 | 0.46 | 0.43 | 0.58 | 1.01 | 35% |
| SDK session 2024-11-05 | 949 | 1.01 | 0.80 | 1.05 | 2.47 | 15% |
| SDK session 2025-03-26 | 977 | 0.98 | 0.80 | 0.96 | 2.46 | 16% |
| SDK session 2025-06-18 | 941 | 1.02 | 0.80 | 1.27 | 2.55 | 15% |
| SDK session 2025-11-25 | 959 | 1.00 | 0.80 | 0.90 | 1.99 | 16% |
| SDK session 2026-07-28 | 2135 | 0.46 | 0.42 | 0.48 | 1.02 | 35% |
| aiohttp, no protocol | 10608 | 0.09 | 0.08 | 0.11 | 0.23 | 100% |
| uvicorn, no protocol | 6123 | 0.16 | 0.15 | 0.20 | 0.32 | 100% |

### Client, add (model result)

| Pairing | Calls/s | Mean (ms) | p50 (ms) | p95 (ms) | p99 (ms) |
| --- | ---: | ---: | ---: | ---: | ---: |
| raw aiohttp -> aiohttp-tiny-mcp server | 6104 | 0.16 | 0.14 | 0.17 | 0.71 |
| raw aiohttp -> SDK stateless | 768 | 1.26 | 1.03 | 1.64 | 2.58 |
| raw aiohttp -> SDK session | 912 | 1.05 | 0.91 | 1.72 | 3.02 |
| aiohttp-tiny-mcp 2024-11-05 -> aiohttp-tiny-mcp server | 4794 | 0.20 | 0.19 | 0.23 | 0.36 |
| aiohttp-tiny-mcp 2025-03-26 -> aiohttp-tiny-mcp server | 4779 | 0.20 | 0.19 | 0.22 | 0.57 |
| aiohttp-tiny-mcp 2025-06-18 -> aiohttp-tiny-mcp server | 4650 | 0.21 | 0.18 | 0.27 | 0.61 |
| aiohttp-tiny-mcp 2025-11-25 -> aiohttp-tiny-mcp server | 4742 | 0.20 | 0.18 | 0.25 | 0.76 |
| aiohttp-tiny-mcp 2026-07-28 -> aiohttp-tiny-mcp server | 4179 | 0.23 | 0.21 | 0.28 | 0.61 |
| aiohttp-tiny-mcp 2024-11-05 -> SDK stateless | 790 | 1.23 | 0.96 | 1.50 | 2.74 |
| aiohttp-tiny-mcp 2025-03-26 -> SDK stateless | 856 | 1.12 | 0.93 | 1.43 | 2.43 |
| aiohttp-tiny-mcp 2025-06-18 -> SDK stateless | 916 | 1.06 | 0.94 | 1.58 | 2.56 |
| aiohttp-tiny-mcp 2025-11-25 -> SDK stateless | 839 | 1.16 | 0.95 | 1.47 | 2.03 |
| aiohttp-tiny-mcp 2026-07-28 -> SDK stateless | 1211 | 0.81 | 0.74 | 1.30 | 2.08 |
| aiohttp-tiny-mcp 2024-11-05 -> SDK session | 1005 | 0.94 | 0.82 | 1.62 | 2.45 |
| aiohttp-tiny-mcp 2025-03-26 -> SDK session | 981 | 0.91 | 0.81 | 1.14 | 2.41 |
| aiohttp-tiny-mcp 2025-06-18 -> SDK session | 1064 | 0.91 | 0.80 | 1.04 | 2.06 |
| aiohttp-tiny-mcp 2025-11-25 -> SDK session | 939 | 1.03 | 0.81 | 1.96 | 2.68 |
| aiohttp-tiny-mcp 2026-07-28 -> SDK session | 1289 | 0.76 | 0.71 | 0.91 | 1.59 |
| official SDK 2025-11-25 -> aiohttp-tiny-mcp server | 1454 | 0.67 | 0.64 | 0.79 | 1.26 |
| official SDK 2025-11-25 -> SDK stateless | 506 | 1.94 | 1.68 | 2.43 | 4.60 |
| official SDK 2025-11-25 -> SDK session | 539 | 1.82 | 1.57 | 2.41 | 3.71 |

### Client, text (string result)

| Pairing | Calls/s | Mean (ms) | p50 (ms) | p95 (ms) | p99 (ms) |
| --- | ---: | ---: | ---: | ---: | ---: |
| raw aiohttp -> aiohttp-tiny-mcp server | 5981 | 0.16 | 0.14 | 0.27 | 0.51 |
| raw aiohttp -> SDK stateless | 762 | 1.26 | 1.02 | 1.82 | 2.58 |
| raw aiohttp -> SDK session | 892 | 1.08 | 0.88 | 1.15 | 2.33 |
| aiohttp-tiny-mcp 2024-11-05 -> aiohttp-tiny-mcp server | 5031 | 0.19 | 0.18 | 0.22 | 0.35 |
| aiohttp-tiny-mcp 2025-03-26 -> aiohttp-tiny-mcp server | 5154 | 0.19 | 0.18 | 0.21 | 0.28 |
| aiohttp-tiny-mcp 2025-06-18 -> aiohttp-tiny-mcp server | 5203 | 0.18 | 0.18 | 0.20 | 0.32 |
| aiohttp-tiny-mcp 2025-11-25 -> aiohttp-tiny-mcp server | 5045 | 0.19 | 0.18 | 0.22 | 0.34 |
| aiohttp-tiny-mcp 2026-07-28 -> aiohttp-tiny-mcp server | 4620 | 0.21 | 0.20 | 0.25 | 0.34 |
| aiohttp-tiny-mcp 2024-11-05 -> SDK stateless | 873 | 1.11 | 0.96 | 1.32 | 2.46 |
| aiohttp-tiny-mcp 2025-03-26 -> SDK stateless | 883 | 1.09 | 0.92 | 1.49 | 2.21 |
| aiohttp-tiny-mcp 2025-06-18 -> SDK stateless | 905 | 1.07 | 0.92 | 1.20 | 1.88 |
| aiohttp-tiny-mcp 2025-11-25 -> SDK stateless | 868 | 1.12 | 0.94 | 1.46 | 2.09 |
| aiohttp-tiny-mcp 2026-07-28 -> SDK stateless | 1531 | 0.64 | 0.62 | 0.71 | 1.18 |
| aiohttp-tiny-mcp 2024-11-05 -> SDK session | 997 | 0.97 | 0.81 | 1.10 | 2.72 |
| aiohttp-tiny-mcp 2025-03-26 -> SDK session | 1075 | 0.90 | 0.80 | 1.13 | 2.31 |
| aiohttp-tiny-mcp 2025-06-18 -> SDK session | 992 | 0.98 | 0.79 | 1.34 | 2.97 |
| aiohttp-tiny-mcp 2025-11-25 -> SDK session | 1094 | 0.85 | 0.78 | 1.00 | 1.45 |
| aiohttp-tiny-mcp 2026-07-28 -> SDK session | 1548 | 0.63 | 0.59 | 0.75 | 1.37 |
| official SDK 2025-11-25 -> aiohttp-tiny-mcp server | 1581 | 0.62 | 0.59 | 0.72 | 1.27 |
| official SDK 2025-11-25 -> SDK stateless | 521 | 1.88 | 1.65 | 2.27 | 4.75 |
| official SDK 2025-11-25 -> SDK session | 568 | 1.73 | 1.53 | 2.07 | 3.24 |

### Stdio, add (model result)

| Pairing | Calls/s | Mean (ms) | p50 (ms) | p95 (ms) | p99 (ms) |
| --- | ---: | ---: | ---: | ---: | ---: |
| raw pipe -> no protocol | 47734 | 0.02 | 0.02 | 0.02 | 0.03 |
| aiohttp-tiny-mcp 2024-11-05 -> aiohttp-tiny-mcp | 12770 | 0.07 | 0.06 | 0.09 | 0.20 |
| aiohttp-tiny-mcp 2025-03-26 -> aiohttp-tiny-mcp | 12379 | 0.08 | 0.06 | 0.10 | 0.31 |
| aiohttp-tiny-mcp 2025-06-18 -> aiohttp-tiny-mcp | 12403 | 0.06 | 0.05 | 0.09 | 0.23 |
| aiohttp-tiny-mcp 2025-11-25 -> aiohttp-tiny-mcp | 12576 | 0.07 | 0.05 | 0.10 | 0.35 |
| aiohttp-tiny-mcp 2026-07-28 -> aiohttp-tiny-mcp | 11480 | 0.08 | 0.06 | 0.13 | 0.41 |
| official SDK 2025-11-25 -> aiohttp-tiny-mcp | 6567 | 0.14 | 0.11 | 0.18 | 0.67 |
| aiohttp-tiny-mcp 2024-11-05 -> SDK | 3064 | 0.32 | 0.30 | 0.44 | 0.65 |
| aiohttp-tiny-mcp 2025-03-26 -> SDK | 3081 | 0.32 | 0.28 | 0.45 | 0.75 |
| aiohttp-tiny-mcp 2025-06-18 -> SDK | 2957 | 0.33 | 0.29 | 0.58 | 0.87 |
| aiohttp-tiny-mcp 2025-11-25 -> SDK | 2862 | 0.34 | 0.32 | 0.48 | 0.87 |
| aiohttp-tiny-mcp 2026-07-28 -> SDK | 2508 | 0.39 | 0.36 | 0.54 | 0.91 |
| official SDK 2025-11-25 -> SDK | 2203 | 0.44 | 0.40 | 0.66 | 1.03 |

### Stdio, text (string result)

| Pairing | Calls/s | Mean (ms) | p50 (ms) | p95 (ms) | p99 (ms) |
| --- | ---: | ---: | ---: | ---: | ---: |
| raw pipe -> no protocol | 52581 | 0.02 | 0.02 | 0.02 | 0.02 |
| aiohttp-tiny-mcp 2024-11-05 -> aiohttp-tiny-mcp | 15950 | 0.06 | 0.05 | 0.07 | 0.15 |
| aiohttp-tiny-mcp 2025-03-26 -> aiohttp-tiny-mcp | 17989 | 0.05 | 0.05 | 0.06 | 0.12 |
| aiohttp-tiny-mcp 2025-06-18 -> aiohttp-tiny-mcp | 19056 | 0.05 | 0.04 | 0.05 | 0.10 |
| aiohttp-tiny-mcp 2025-11-25 -> aiohttp-tiny-mcp | 18744 | 0.05 | 0.04 | 0.06 | 0.16 |
| aiohttp-tiny-mcp 2026-07-28 -> aiohttp-tiny-mcp | 14424 | 0.06 | 0.05 | 0.07 | 0.21 |
| official SDK 2025-11-25 -> aiohttp-tiny-mcp | 9293 | 0.10 | 0.09 | 0.12 | 0.29 |
| aiohttp-tiny-mcp 2024-11-05 -> SDK | 2704 | 0.36 | 0.30 | 0.65 | 1.31 |
| aiohttp-tiny-mcp 2025-03-26 -> SDK | 2763 | 0.35 | 0.32 | 0.59 | 1.01 |
| aiohttp-tiny-mcp 2025-06-18 -> SDK | 2950 | 0.33 | 0.31 | 0.47 | 1.02 |
| aiohttp-tiny-mcp 2025-11-25 -> SDK | 2735 | 0.36 | 0.32 | 0.63 | 0.97 |
| aiohttp-tiny-mcp 2026-07-28 -> SDK | 2388 | 0.41 | 0.37 | 0.68 | 1.15 |
| official SDK 2025-11-25 -> SDK | 2414 | 0.40 | 0.36 | 0.58 | 0.98 |

### Server, 32 calls in flight

| Pairing | Calls/s | Mean (ms) | p50 (ms) | p95 (ms) | p99 (ms) | Of floor |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| aiohttp-tiny-mcp 2024-11-05 | 8323 | 3.21 | 3.07 | 3.86 | 10.29 | 59% |
| aiohttp-tiny-mcp 2025-03-26 | 8340 | 3.07 | 3.01 | 3.67 | 5.06 | 59% |
| aiohttp-tiny-mcp 2025-06-18 | 8594 | 3.04 | 2.93 | 3.84 | 6.28 | 61% |
| aiohttp-tiny-mcp 2025-11-25 | 8446 | 3.07 | 2.99 | 3.53 | 5.70 | 60% |
| aiohttp-tiny-mcp 2026-07-28 | 8140 | 3.21 | 3.12 | 3.87 | 5.61 | 58% |
| SDK stateless 2024-11-05 | 1007 | 30.20 | 28.36 | 51.98 | 77.62 | 13% |
| SDK stateless 2025-03-26 | 1016 | 29.46 | 23.91 | 69.47 | 104.87 | 13% |
| SDK stateless 2025-06-18 | 949 | 31.81 | 25.19 | 90.38 | 120.52 | 12% |
| SDK stateless 2025-11-25 | 913 | 33.12 | 28.20 | 65.75 | 159.26 | 12% |
| SDK stateless 2026-07-28 | 1599 | 18.36 | 15.65 | 42.80 | 79.26 | 20% |
| SDK session 2024-11-05 | 809 | 39.10 | 36.11 | 55.40 | 155.99 | 10% |
| SDK session 2025-03-26 | 827 | 38.20 | 34.95 | 52.15 | 188.42 | 10% |
| SDK session 2025-06-18 | 795 | 39.23 | 36.26 | 50.66 | 197.07 | 10% |
| SDK session 2025-11-25 | 755 | 41.70 | 36.05 | 57.76 | 306.89 | 10% |
| SDK session 2026-07-28 | 1530 | 19.12 | 16.94 | 36.57 | 57.64 | 19% |
| aiohttp, no protocol | 14112 | 1.62 | 1.48 | 2.50 | 5.77 | 100% |
| uvicorn, no protocol | 7879 | 3.31 | 2.88 | 5.97 | 12.54 | 100% |

### What a schema costs

```text
No HTTP. Answer sizes are the bytes the server would write.

small schema
2024-11-05 tools/call                  46801 ops/s     21.4 us    104 B
2024-11-05 tools/list                  78218 ops/s     12.8 us    303 B
2025-03-26 tools/call                  57804 ops/s     17.3 us    104 B
2025-03-26 tools/list                  79616 ops/s     12.6 us    303 B
2025-06-18 tools/call                  65133 ops/s     15.4 us    137 B
2025-06-18 tools/list                  77070 ops/s     13.0 us    489 B
2025-11-25 tools/call                  61603 ops/s     16.2 us    137 B
2025-11-25 tools/list                  74117 ops/s     13.5 us    489 B
2026-07-28 tools/call                  46903 ops/s     21.3 us    249 B
2026-07-28 tools/list                  58353 ops/s     17.1 us    634 B

fat schema
2024-11-05 tools/call                  52827 ops/s     18.9 us    175 B
2024-11-05 tools/list                  40846 ops/s     24.5 us   2071 B
2025-03-26 tools/call                  51318 ops/s     19.5 us    175 B
2025-03-26 tools/list                  38908 ops/s     25.7 us   2071 B
2025-06-18 tools/call                  55467 ops/s     18.0 us    259 B
2025-06-18 tools/list                  30158 ops/s     33.2 us   2999 B
2025-11-25 tools/call                  58259 ops/s     17.2 us    259 B
2025-11-25 tools/list                  25572 ops/s     39.1 us   2999 B
2026-07-28 tools/call                  36188 ops/s     27.6 us    371 B
2026-07-28 tools/list                  28967 ops/s     34.5 us   3069 B
```

The results describe protocol overhead for small handlers. Real deployments also
pay for network latency, TLS, application work, and any shared stores. The
HTTP benchmark reuses one initialized client session per server.
