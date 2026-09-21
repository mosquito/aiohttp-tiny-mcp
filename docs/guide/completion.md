# Completion

Completion suggests values for something a person is filling in: an argument of
a prompt, or a variable of a resource template. It exists so that picking one
is not typing blind.

One handler serves every completion request on the server. It is told what is
being completed and what has been typed so far, and returns what fits.

<!-- name: test_completion -->
```python
from pydantic import BaseModel

from aiohttp_tiny_mcp import MemoryHub, MemorySessionStore, Registry
from aiohttp_tiny_mcp.protocol.models import CompleteParams

registry = Registry("catalog", "1.0", hub=MemoryHub(), session_store=MemorySessionStore())

LANGUAGES = ("python", "rust", "go", "ruby")
REGIONS = ("eu-west", "eu-north", "us-east")


class Review(BaseModel):
    language: str


@registry.prompt
async def review(args: Review) -> str:
    """Review code in a given language."""
    return f"Review this {args.language} code."


class Deployment(BaseModel):
    region: str


@registry.resource("deploy://regions/{region}", name="region")
async def region(args: Deployment) -> dict:
    """One region's state."""
    return {"region": args.region, "healthy": True}


@registry.completions
async def complete(args: CompleteParams) -> list[str]:
    """Suggest values, whatever is being filled in."""
    typed = args.argument.value
    if args.argument.name == "language":
        return [name for name in LANGUAGES if name.startswith(typed)]
    if args.argument.name == "region":
        return [name for name in REGIONS if name.startswith(typed)]
    return []
```

## What the handler is told

`CompleteParams` carries two things.

`ref` says what is being completed -- `{"type": "ref/prompt", "name": ...}` for
a prompt argument, `{"type": "ref/resource", "uri": ...}` for a template
variable. Read it where the same argument name means different things in
different places.

`argument` says which argument and what has been typed: `name` and `value`.
`value` is a prefix and may be empty, which is the case for "show me
everything".

<!-- name: async test_completion; fixtures: serve -->
```python
from aiohttp_tiny_mcp import Client
from aiohttp_tiny_mcp.protocol.selection import AdapterSet

url = await serve(registry)
async with Client(url, AdapterSet.default().by_version["2026-07-28"]) as client:
    await client.initialize()

    languages = await client.complete(
        {"type": "ref/prompt", "name": "review"},
        {"name": "language", "value": "r"},
    )
    regions = await client.complete(
        {"type": "ref/resource", "uri": "deploy://regions/{region}"},
        {"name": "region", "value": "eu"},
    )
    everything = await client.complete(
        {"type": "ref/prompt", "name": "review"},
        {"name": "language", "value": ""},
    )

assert languages.completion.values == ["rust", "ruby"]
assert regions.completion.values == ["eu-west", "eu-north"]
assert len(everything.completion.values) == 4
```

## Returning more than a list

Return a `Completion` to say there are more matches than were sent. A client
shows that as "and 900 more" rather than pretending the list is complete.

<!-- name: test_completion -->
```python
from aiohttp_tiny_mcp.protocol.models import Completion


async def complete_many(args: CompleteParams) -> Completion:
    matches = [f"item-{n}" for n in range(1000)]
    return Completion(values=matches[:100], total=len(matches), has_more=True)
```

A bare list is truncated to a hundred values, which is the specification's
limit, and reported as the total.

## Filtering yourself

Nothing filters for you. `value` is what has been typed, and a handler that
ignores it returns everything -- which is correct for an empty prefix and wrong
for anything else.

Prefix matching is the usual behaviour, and what a person expects. Match
anywhere in the string if that suits your values better; just be consistent, so
the suggestions do not appear to change rules.

## No handler at all

Completion is optional. Without a handler the server does not advertise the
capability, and a request for one answers with an empty list rather than an
error -- a client that asks anyway gets a usable answer.
