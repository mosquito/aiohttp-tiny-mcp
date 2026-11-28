# Cache hints

:::{important}
Cache hints take effect only on MCP revision `2026-07-28`. On every older
revision, the server accepts the same cache settings but omits `ttlMs` and
`cacheScope` from responses.
:::

A client must implement caching to act on these hints. The server runs the
handler for every request it receives. If you need to reuse results on the
server, implement that cache in your application. MCP hints and HTTP
`Cache-Control` are separate mechanisms.

- `ttlMs` is the freshness lifetime in milliseconds. Use `0` when the client
  should fetch a fresh result each time.
- `cacheScope: "private"` limits reuse to a private cache. Use it for results
  that depend on identity, permissions, or session state.
- `cacheScope: "public"` allows shared reuse. Use it only when the same request
  returns the same content to every caller who can make it. Keep listings
  private if their contents depend on permissions.

## Set the TTL on a resource

Pass `cache_ttl` directly to `@registry.resource(...)` as seconds. Integers
and floats are accepted, so `cache_ttl=0.25` yields `ttlMs: 250`. The server
rounds down to whole milliseconds. The same option
works for fixed URIs and templates. Here the public schema stays fresh for
five minutes, and the live status sets a TTL of zero.

<!-- name: async test_cache_hint_configuration -->
```python
from pydantic import BaseModel

from aiohttp_tiny_mcp import Registry
from aiohttp_tiny_mcp.testing import over_http


class Nothing(BaseModel):
    pass


registry = Registry("catalogue", "1.0")


@registry.resource("catalogue://schema", cache_ttl=300, cache_scope="public")
async def schema(args: Nothing) -> str:
    """Return the schema shared by all callers."""
    return "schema version 3"


@registry.resource("catalogue://status", cache_ttl=0)
async def status(args: Nothing) -> str:
    """Return a status that the client must refresh on each read."""
    return "ready"


async with over_http(registry, adapter="2026-07-28") as client:
    schema_result = await client.read_resource("catalogue://schema")
    status_result = await client.read_resource("catalogue://status")

assert (schema_result.ttl_ms, schema_result.cache_scope) == (300_000, "public")
assert (status_result.ttl_ms, status_result.cache_scope) == (0, "private")
```

## Compute the TTL during a read

Add a parameter annotated with `Cache` to the resource handler. Its `ttl`
starts at the resource declaration, the registry default for `resources/read`,
or zero. Assign `cache.ttl`, call `cache.set_ttl(seconds)`, or increment it
with `+=`. The change applies only to this read. Older revisions still inject
`Cache`, so the same handler runs, but its TTL does not appear in the reply.

<!-- name: async test_cache_hint_per_read -->
```python
from pydantic import BaseModel

from aiohttp_tiny_mcp import Cache, Registry
from aiohttp_tiny_mcp.testing import over_http


class Item(BaseModel):
    name: str


registry = Registry("inventory", "1.0")


@registry.resource("inventory://{name}", cache_ttl=1.0)
async def item(args: Item, cache: Cache) -> str:
    """Return an item with a lifetime based on its name."""
    if args.name == "preview":
        cache.set_ttl(0)
    else:
        cache.ttl += 0.5
    return args.name


async with over_http(registry, adapter="2026-07-28") as client:
    preview = await client.read_resource("inventory://preview")
    first = await client.read_resource("inventory://item")
    second = await client.read_resource("inventory://item")

assert preview.ttl_ms == 0
assert first.ttl_ms == second.ttl_ms == 1_500
```

## How the server selects a hint

The server looks for the first non-`None` value for each field separately:

1. A value set during the read, or set by an extension method on its `CacheableResult`.
2. The declaration of the resource or template being read.
3. The registry default for the method.
4. The built-in defaults of `ttlMs: 0` and `cacheScope: "private"`.

Resource handlers return content and can adjust the TTL through `Cache`. An [extension method](extensions.md) can return a subclass of
`CacheableResult` to set its own hints per response.

An explicit `0` overrides any positive TTL. A resource with required scopes
always returns `private`. Declaring `cache_scope="public"` on such a resource
is a registration error.

## Set defaults per method

Use `Registry(cache_hints=...)` for listings and for resources that leave
one or both fields unset. `CacheHint(cache_ttl=...)` also takes seconds.

<!-- name: test_cache_hint_defaults -->
```python
from aiohttp_tiny_mcp import CacheHint, Registry

registry = Registry(
    "catalogue",
    "1.0",
    cache_hints={
        "tools/list": CacheHint(cache_ttl=10, scope="private"),
        "resources/read": CacheHint(cache_ttl=30),
    },
)
```

The supported keys for `cache_hints` are `server/discover`, `tools/list`,
`prompts/list`, `resources/list`, `resources/templates/list`, and
`resources/read`. Other keys, such as `tools/call`, raise `ValueError` when the
registry is constructed. Hints for `resources/read` apply to reads, while hints
for `resources/list` apply to the listing itself.
