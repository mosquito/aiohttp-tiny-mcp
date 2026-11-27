# What it costs

This page is the motivation for the library, stated in numbers rather than in
adjectives. It compares this package with the official Python SDK on one
`tools/call`, per protocol revision, for the server and for the client
separately, over HTTP and over a pipe.

The harness lives in `benchmarks/` in the source tree and is not installed
with the package. Reproduce any row with:

```bash
uv run python -m benchmarks.run --suite server --operations call text list
uv run python -m benchmarks.run --suite client
uv run python -m benchmarks.run --suite stdio
uv run python -m benchmarks.run --suite server --operations call --concurrency 32 --calls 3000 --repeats 2
uv run python -m benchmarks.schema_cost       # one request, without a socket
```

## How to read it

Both libraries serve the same tool -- add two integers, return a model. What
differs is the protocol machinery between the socket and that function.

The official SDK row is its current high-level server (`MCPServer` in
`mcp==2.2.0`; older SDK releases called the comparable API `FastMCP`). This is
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

Measured on an Arch Linux x86-64 VM with 16 vCPUs on an AMD Ryzen AI 9 HX PRO
370, kernel `7.2.11-arch1-1`. CPython 3.14.2, aiohttp 3.14.5, Pydantic 2.14.4,
MCP SDK 2.2.0, uvicorn 0.54.0, and Starlette 1.10.0. Both HTTP listeners use
`TCP_NODELAY`. The driver and servers run on the same machine over loopback,
without TLS. Logging is set to ERROR and HTTP access logs are disabled.

The serial runs use 1,500 measured calls, 200 warmup calls, and three rotated
rounds per row. The concurrent run uses 3,000 calls, 200 warmup calls, two
rounds, and up to 32 calls in flight. Each table reports the fastest round
per row and the latency percentiles from that round.

## Server over HTTP, per revision

add (model result), calls per second.

| Server | 2024-11-05 | 2025-03-26 | 2025-06-18 | 2025-11-25 | 2026-07-28 |
| --- | ---: | ---: | ---: | ---: | ---: |
| aiohttp-tiny-mcp | 5745 | 6001 | 6037 | 6248 | 5956 |
| SDK session | 852 | 824 | 846 | 853 | 1404 |
| SDK stateless | 802 | 776 | 726 | 752 | 1347 |
| aiohttp, no protocol | 10411 | — | — | — | — |
| uvicorn, no protocol | 6329 | — | — | — | — |

text (string result), calls per second.

| Server | 2024-11-05 | 2025-03-26 | 2025-06-18 | 2025-11-25 | 2026-07-28 |
| --- | ---: | ---: | ---: | ---: | ---: |
| aiohttp-tiny-mcp | 6263 | 5610 | 6744 | 6785 | 6148 |
| SDK session | 812 | 873 | 859 | 866 | 1711 |
| SDK stateless | 740 | 777 | 753 | 775 | 1667 |
| aiohttp, no protocol | 9821 | — | — | — | — |
| uvicorn, no protocol | 6310 | — | — | — | — |

tools/list, calls per second.

| Server | 2024-11-05 | 2025-03-26 | 2025-06-18 | 2025-11-25 | 2026-07-28 |
| --- | ---: | ---: | ---: | ---: | ---: |
| aiohttp-tiny-mcp | 6379 | 6568 | 6307 | 6406 | 6028 |
| SDK session | 949 | 977 | 941 | 959 | 2135 |
| SDK stateless | 892 | 862 | 830 | 823 | 2155 |
| aiohttp, no protocol | 10608 | — | — | — | — |
| uvicorn, no protocol | 6123 | — | — | — | — |

### At 32 calls in flight

| Server | Calls/s across revisions | p99 across revisions (ms) |
| --- | ---: | ---: |
| aiohttp-tiny-mcp | 8140–8594 | 5.06–10.29 |
| SDK session | 755–1530 | 57.64–306.89 |
| SDK stateless | 913–1599 | 77.62–159.26 |
| aiohttp, no protocol | 14112 | 5.77 |
| uvicorn, no protocol | 7879 | 12.54 |

## Client over HTTP

Model results, calls per second. The server stays constant within each column.

| Client | aiohttp-tiny-mcp server | SDK stateless | SDK session |
| --- | ---: | ---: | ---: |
| raw aiohttp | 6104 | 768 | 912 |
| aiohttp-tiny-mcp 2024-11-05 | 4794 | 790 | 1005 |
| aiohttp-tiny-mcp 2025-03-26 | 4779 | 856 | 981 |
| aiohttp-tiny-mcp 2025-06-18 | 4650 | 916 | 1064 |
| aiohttp-tiny-mcp 2025-11-25 | 4742 | 839 | 939 |
| aiohttp-tiny-mcp 2026-07-28 | 4179 | 1211 | 1289 |
| official SDK 2025-11-25 | 1454 | 506 | 539 |

## Over stdio

One subprocess and one pipe. Calls per second.

| Pairing | Model result | String result |
| --- | ---: | ---: |
| raw pipe -> no protocol | 47734 | 52581 |
| aiohttp-tiny-mcp 2024-11-05 -> aiohttp-tiny-mcp | 12770 | 15950 |
| aiohttp-tiny-mcp 2025-03-26 -> aiohttp-tiny-mcp | 12379 | 17989 |
| aiohttp-tiny-mcp 2025-06-18 -> aiohttp-tiny-mcp | 12403 | 19056 |
| aiohttp-tiny-mcp 2025-11-25 -> aiohttp-tiny-mcp | 12576 | 18744 |
| aiohttp-tiny-mcp 2026-07-28 -> aiohttp-tiny-mcp | 11480 | 14424 |
| official SDK 2025-11-25 -> aiohttp-tiny-mcp | 6567 | 9293 |
| aiohttp-tiny-mcp 2024-11-05 -> SDK | 3064 | 2704 |
| aiohttp-tiny-mcp 2025-03-26 -> SDK | 3081 | 2763 |
| aiohttp-tiny-mcp 2025-06-18 -> SDK | 2957 | 2950 |
| aiohttp-tiny-mcp 2025-11-25 -> SDK | 2862 | 2735 |
| aiohttp-tiny-mcp 2026-07-28 -> SDK | 2508 | 2388 |
| official SDK 2025-11-25 -> SDK | 2203 | 2414 |

## Scope of the results

These measurements cover loopback calls with small handlers. They do not
measure TLS, a remote database, a shared session store, a shared hub, or
many independently initialized clients. The fastest rotated round is
optimistic. Compare rows in the same run.

The [full benchmark tables](https://github.com/mosquito/aiohttp-tiny-mcp/blob/master/benchmarks/README.md#results)
include latency percentiles, string results for HTTP clients, and a schema
benchmark with no socket.
