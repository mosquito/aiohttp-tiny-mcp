# aiohttp-tiny-mcp

[![Tests](https://github.com/mosquito/aiohttp-tiny-mcp/actions/workflows/tests.yml/badge.svg)](https://github.com/mosquito/aiohttp-tiny-mcp/actions/workflows/tests.yml)
[![Coverage](https://img.shields.io/coveralls/github/mosquito/aiohttp-tiny-mcp/master)](https://coveralls.io/github/mosquito/aiohttp-tiny-mcp?branch=master)
[![Latest release](https://img.shields.io/github/v/release/mosquito/aiohttp-tiny-mcp)](https://github.com/mosquito/aiohttp-tiny-mcp/releases)
[![License](https://img.shields.io/github/license/mosquito/aiohttp-tiny-mcp)](https://github.com/mosquito/aiohttp-tiny-mcp/blob/master/LICENSE)

`aiohttp-tiny-mcp` turns an aiohttp service into a remote MCP server. Declare
Python handlers as tools, resources, or prompts; the library exposes them over
Streamable HTTP or stdio.

It is designed for services behind a load balancer. A later request from one
MCP client may reach another process, so sessions and events live in backends
shared by all workers. For one process, the included memory backends are used
automatically.

## Start here

1. [Quickstart](quickstart.md) — run a server and call a tool.
2. [Tools, resources, and prompts](concepts.md) — choose what to expose.
3. [How the server fits together](pieces.md) — understand `Exchange`,
   `SessionStore`, and `Hub` before deploying more than one worker.
4. [Authentication](guide/auth.md) — protect a standalone endpoint or mount it
   in an aiohttp application that already verifies JWTs.

The library supports `2024-11-05`, `2025-03-26`, `2025-06-18`, `2025-11-25`,
and `2026-07-28` from one set of handlers. Use the
[compatibility table](reference/parity.md) for protocol differences.

Already use the official Python SDK? Read [how it compares](concepts.md#official-python-sdk)
before choosing a framework. For local benchmark methodology and results, see
[What it costs](reference/benchmarks.md).

```{toctree}
:maxdepth: 2
:caption: Getting started

quickstart
concepts
pieces
```

```{toctree}
:maxdepth: 2
:caption: Writing handlers

guide/tools
guide/resources
guide/prompts
guide/completion
guide/exchange
guide/dependencies
guide/auth
guide/testing
guide/asking
guide/notifications
guide/sessions
```

```{toctree}
:maxdepth: 2
:caption: Calling and running a server

guide/client
guide/console
deployment/transports
deployment/stores
deployment/multitenancy
```

```{toctree}
:maxdepth: 2
:caption: Reference

reference/benchmarks
reference/parity
reference/revisions
reference/adapters
reference/selection
reference/runtime
reference/verification
reference/api
```
