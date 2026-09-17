# Tools

A tool is a function the model may call. See
[Tools, resources, and prompts](../concepts.md) for when a tool is the right shape and when
a resource or a prompt is.

Everything is declared on the `Registry`, once, with no mention of a protocol
revision.

<!-- name: test_tools -->
```python
from pydantic import BaseModel, Field

from aiohttp_tiny_mcp import Hint, MemoryHub, MemorySessionStore, Registry

registry = Registry("catalog", "1.0", hub=MemoryHub(), session_store=MemorySessionStore())
```

## Declaring one

The first argument's annotation is the input schema. The return annotation, if
it is a model, is the output schema. The docstring is the description.

<!-- name: test_tools -->
```python
class Search(BaseModel):
    query: str
    limit: int = Field(10, ge=1, le=100, description="How many to return.")


class Hits(BaseModel):
    found: int
    items: list[str]


@registry.tool
async def search(args: Search) -> Hits:
    """Search the catalog."""
    items = [f"{args.query}-{n}" for n in range(args.limit)]
    return Hits(found=len(items), items=items)
```

The description is not decoration. The model reads it, the name and the schema,
and decides from those alone whether this is the tool for what it is doing.
Write for that reader: say what the tool does, when to use it, and what it
returns.

Field descriptions and constraints go the same way. `Field(..., description=...)`
reaches the model; `ge`/`le` reach both the model and the validator.

## Names, titles and hints

<!-- name: test_tools -->
```python
class Nothing(BaseModel):
    """No arguments. Every handler takes a model, even an empty one."""


@registry.tool(
    name="purge",
    title="Empty the catalog",
    annotations=Hint.DESTRUCTIVE | Hint.IDEMPOTENT,
)
async def wipe_everything(args: Nothing) -> str:
    """Remove every item. This cannot be undone."""
    return "emptied"
```

`name` is what the model calls. `title` is what a person sees. `Hint` carries
what the specification defines, one member per hint, joined with `|`:

| Hint | Says | Where a tool says nothing |
| --- | --- | --- |
| `Hint.READ_ONLY` | Changes nothing | false |
| `Hint.DESTRUCTIVE` | May remove or overwrite | **true** |
| `Hint.IDEMPOTENT` | Calling it twice is the same as once | false |
| `Hint.OPEN_WORLD` | Reaches something outside this server | **true** |

Two of them default to true: the specification assumes the worst about a tool
that says nothing. Deny one with `~`, and pass a single hint on its own:

<!-- name: test_tools -->
```python
@registry.tool(annotations=Hint.READ_ONLY | ~Hint.OPEN_WORLD)
async def count(args: Nothing) -> int:
    """How many items there are. Reads this server and nothing else."""
    return 3
```

They are hints, not enforcement. A host uses them to decide what to confirm
with the person first, which is the point of saying so. A plain mapping is
still accepted, which is how a hint added to the specification later is
written before this package catches up.

## What a tool may return

A model produces an output schema and structured content. Anything else is
rendered as text.

<!-- name: test_tools -->
```python
@registry.tool
async def health(args: Nothing) -> str:
    """One line, no schema."""
    return "ok"
```

| Returned | Becomes |
| --- | --- |
| a pydantic model | `structuredContent` plus text, and an `outputSchema` |
| a string | one text block |
| a number, list or dict | one text block holding its JSON |
| a `CallToolResult` | itself, unchanged |

Build a `CallToolResult` where a tool needs several content blocks, or content
that is not text. A block is `TextContent`, `ImageContent`, `AudioContent`,
`EmbeddedResource`, which carries the resource contents themselves, or
`ResourceLink`, which names a resource to read later. `ResourceLink` arrived in
`2025-06-18`, and the older revisions drop it from the result.

## Failures are results, not errors

A handler that raises does not produce a JSON-RPC error. It produces a tool
result with `isError` set, because the model asked for something and deserves
to be told what happened rather than having the call disappear.

<!-- name: async test_tools; fixtures: serve -->
```python
from aiohttp_tiny_mcp import Client
from aiohttp_tiny_mcp.protocol.selection import AdapterSet


@registry.tool
async def boom(args: Nothing) -> str:
    """Always raises."""
    raise RuntimeError("kaboom")


url = await serve(registry)
async with Client(url, AdapterSet.default().by_version["2026-07-28"]) as client:
    await client.initialize()
    result = await client.call_tool("boom", {})

assert result.is_error is True
assert "kaboom" in result.content[0].text
```

Arguments that fail validation are reported the same way, for the same reason:
the model can read what was wrong and try again.

A genuine protocol failure -- an unknown tool, a malformed request -- is still
a JSON-RPC error, because there the call never reached a tool at all.

## Without a decorator

Every declaration takes the handler as an argument too, and the two forms do
the same thing.

<!-- name: test_registration -->
```python
from pydantic import BaseModel

from aiohttp_tiny_mcp import MemoryHub, MemorySessionStore, Registry


class Nothing(BaseModel):
    pass


class Add(BaseModel):
    a: int
    b: int


async def add(args: Add) -> int:
    """Add two integers."""
    return args.a + args.b


async def config(args: Nothing) -> dict:
    """Application configuration."""
    return {"debug": False}


plain = Registry("plain", "1.0", hub=MemoryHub(), session_store=MemorySessionStore())

plain.tool(add)
plain.tool(add, name="sum", title="Add up")
plain.resource("config://app", config, mime_type="application/json")

assert set(plain.tools) == {"add", "sum"}
```

A decorator has to stand where the function is written. The argument form does
not, which is what it is for:

**A handler defined somewhere else.** Collect the functions in the modules they
belong to, and assemble the server in one place, with no import-time side
effects and no registry that has to exist before them.

**A handler that holds its own state.** A method is bound to an object built at
run time, which no decorator can reach.

<!-- name: async test_registration; fixtures: serve -->
```python
from aiohttp_tiny_mcp import Client
from aiohttp_tiny_mcp.protocol.selection import AdapterSet


class Counter:
    def __init__(self, start: int) -> None:
        self.total = start

    async def bump(self, args: Nothing) -> int:
        """Increase and report."""
        self.total += 1
        return self.total


counter = Counter(41)
plain.tool(counter.bump, name="bump")

url = await serve(plain)
async with Client(url, AdapterSet.default().by_version["2026-07-28"]) as client:
    await client.initialize()
    result = await client.call_tool("bump", {})

assert result.content[0].text == "42"
assert counter.total == 42
```

**A handler chosen at run time.** Register one implementation or another
depending on configuration, without writing both and hiding one.

The name comes from the function unless `name=` says otherwise, so the same
handler may be registered more than once under different names.

## Long calls

A tool declared `streaming=True` gets a response stream and can report progress
and log messages before it returns. See
[Notifications](notifications.md#progress).

## Asking the user

A tool that needs a decision asks for one. See
[Asking the user](asking.md).

## Requiring a newer revision

A tool that cannot be expressed on an older revision can say so, and is hidden
there instead of being offered in a broken form.

<!-- name: async test_tools; fixtures: serve -->
```python
@registry.tool(min_revision="2026-07-28")
async def modern_only(args: Nothing) -> str:
    """Offered only where the revision can express it."""
    return "new"


url = await serve(registry)
for version, expected in (("2026-07-28", True), ("2025-03-26", False)):
    async with Client(url, AdapterSet.default().by_version[version]) as client:
        await client.initialize()
        names = {tool.name for tool in await client.list_tools()}
    assert ("modern_only" in names) is expected, version
```

Some degradation happens without being asked for. A schema an older revision
cannot represent is simplified where that is lossless, and the tool is hidden
where it is not -- one decision, in the adapter, rather than a condition in
every handler. See [What varies](../reference/revisions.md#degradation-that-is-not-a-choice).
