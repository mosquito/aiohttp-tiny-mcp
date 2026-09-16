# Testing a server

The helpers in aiohttp_tiny_mcp.testing make an application server test small
without replacing the MCP protocol with mocks.

| Helper | Use it when | What runs |
| --- | --- | --- |
| connect | The handler result is the subject | Real revision encoding, dispatch, and decoding over in-memory streams |
| over_http | Headers, sessions, streams, or origin rules matter | A real aiohttp endpoint and HTTP client on loopback |
| serving | The test needs the endpoint URL itself | A real aiohttp endpoint on a temporary loopback port |
| every_revision | One declaration must work across all supported revisions | connect once per revision |

Copy this body into an async pytest test. It creates a registry, registers a
tool, and calls it through the same protocol path a client uses. No
documentation fixture or open port is involved.

<!-- name: async test_testing -->
```python
from pydantic import BaseModel

from aiohttp_tiny_mcp import Registry
from aiohttp_tiny_mcp.testing import connect, every_revision


class Add(BaseModel):
    a: int
    b: int


class Sum(BaseModel):
    total: int


registry = Registry("calculator", "1.0")


@registry.tool
async def add(args: Add) -> Sum:
    """Add two integers."""
    return Sum(total=args.a + args.b)


async with connect(registry) as client:
    result = await client.call_tool("add", {"a": 2, "b": 3})

assert result.structured_content == {"total": 5}


results = await every_revision(
    registry,
    lambda client: client.call_tool("add", {"a": 2, "b": 3}),
)

assert {version: result.structured_content for version, result in results.items()} == {
    "2026-07-28": {"total": 5},
    "2025-11-25": {"total": 5},
    "2025-06-18": {"total": 5},
    "2025-03-26": {"total": 5},
    "2024-11-05": {"total": 5},
}
```

The helpers are async context managers. They close streams, HTTP clients, and
temporary servers when the block exits. Use a production Client and a deployed
URL in integration tests that must cover your proxy, TLS, or load balancer.

## Test the HTTP boundary

Use over_http when the request itself is part of the behaviour. For example,
this sends the header through a real aiohttp request:

<!-- name: async test_testing_http -->
```python
from pydantic import BaseModel

from aiohttp_tiny_mcp import Exchange, Registry
from aiohttp_tiny_mcp.testing import over_http


class Nothing(BaseModel):
    pass


registry = Registry("headers", "1.0")


@registry.tool
async def request_id(args: Nothing, ex: Exchange) -> str:
    """Read the request identifier set by an HTTP caller."""
    return ex.request.headers["X-Request-Id"]


async with over_http(registry, headers={"X-Request-Id": "example-42"}) as client:
    result = await client.call_tool("request_id", {})

assert result.content[0].text == "example-42"
```

The in-memory connect helper deliberately has no HTTP request, so use over_http
for middleware, authentication, headers, sessions, response streams, and
origin checks.
