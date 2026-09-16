# aiohttp-tiny-mcp

[![Tests](https://github.com/mosquito/aiohttp-tiny-mcp/actions/workflows/tests.yml/badge.svg)](https://github.com/mosquito/aiohttp-tiny-mcp/actions/workflows/tests.yml)
[![PyPI version](https://img.shields.io/pypi/v/aiohttp-tiny-mcp.svg)](https://pypi.org/project/aiohttp-tiny-mcp/)
[![License](https://img.shields.io/pypi/l/aiohttp-tiny-mcp.svg)](https://github.com/mosquito/aiohttp-tiny-mcp/blob/master/LICENSE)
[![Python versions](https://img.shields.io/pypi/pyversions/aiohttp-tiny-mcp.svg)](https://pypi.org/project/aiohttp-tiny-mcp/)
[![Documentation](https://img.shields.io/badge/docs-online-blue.svg)](https://mosquito.github.io/aiohttp-tiny-mcp/)

[Documentation](https://mosquito.github.io/aiohttp-tiny-mcp/) ·
[Repository](https://github.com/mosquito/aiohttp-tiny-mcp) ·
[Issues](https://github.com/mosquito/aiohttp-tiny-mcp/issues) ·
[PyPI](https://pypi.org/project/aiohttp-tiny-mcp/)

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
