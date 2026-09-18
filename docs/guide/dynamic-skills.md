# Dynamic skills

Async extension handlers can read current instructions from a database or
generate them for each request. `Skills.from_directory()` provides a startup
snapshot; it does not watch files or reload their contents.

## Choose a manifest strategy

Changing content does not by itself require a dynamic manifest. If you can
keep resource bytes consistent with their published hashes, return the full
manifest with sizes and digests. An immutable, versioned snapshot is one way
to maintain that consistency.

When stable digests cannot be published, return `resources: "dynamic"` in each
skill entry. Clients may decline to load these skills. The entry's frontmatter
must still match the YAML frontmatter in the returned `SKILL.md`. These are
requirements of the
[Skills specification](https://github.com/modelcontextprotocol/ext-skills/blob/main/specification/stable/skills.mdx#resources).

Use `ttlMs: 0` for results that must be fetched again, and keep
`cacheScope: "private"` for caller-specific content. `CacheableResult` supplies
these defaults on `2026-07-28`.

## Generate content at a fixed URI

This example registers one skill whose instructions change while the server
runs. The source is an injected object; replace it with an async database
provider when needed. No filesystem or YAML parser is required here.

<!-- name: async test_dynamic_skills -->
```python
from pydantic import BaseModel

from aiohttp_tiny_mcp import Extension, Registry
from aiohttp_tiny_mcp.core import Failure, FailureKind, Rejected
from aiohttp_tiny_mcp.models import CacheableResult, ListParams, ReadResourceParams
from aiohttp_tiny_mcp.testing import connect

URI = "skill://deploy/SKILL.md"
DESCRIPTION = "Deploy using current release instructions."


class Nothing(BaseModel):
    pass


class Instructions:
    def __init__(self, text: str):
        self.text = text


class Listing(CacheableResult):
    skills: list[dict]


class Detail(CacheableResult):
    skill: dict


def entry() -> dict:
    return {
        "uri": URI,
        "frontmatter": {"name": "deploy", "description": DESCRIPTION},
        "resources": "dynamic",
    }


extension = Extension("io.modelcontextprotocol/skills")


@extension.method("skills/list")
async def list_skills(args: ListParams) -> Listing:
    if args.cursor is not None:
        raise Rejected(Failure(FailureKind.INVALID_PARAMS, "Unknown cursor"))
    return Listing(skills=[entry()])


@extension.method("skills/get")
async def get_skill(args: ReadResourceParams) -> Detail:
    if args.uri != URI:
        raise Rejected(Failure(FailureKind.INVALID_PARAMS, "Unknown skill"))
    return Detail(skill=entry())


@extension.resource(URI, mime_type="text/markdown", cache_ttl_ms=0, cache_scope="private")
async def read_skill(args: Nothing, source: Instructions) -> str:
    return f"---\nname: deploy\ndescription: {DESCRIPTION}\n---\n{source.text}\n"


source = Instructions("Deploy the blue release using the deployment tool.")
registry = Registry("live-skills", "1.0")
registry.provide_instance(source)
registry.extension(extension)

async with connect(registry, adapter="2026-07-28") as client:
    discovery = await client.initialize()
    assert "io.modelcontextprotocol/skills" in discovery["capabilities"]["extensions"]
    listing = await client.request_method("skills/list")
    assert listing["skills"][0]["resources"] == "dynamic"
    assert listing["ttlMs"] == 0
    assert listing["cacheScope"] == "private"
    detail = await client.request_method("skills/get", {"uri": URI})
    assert detail["skill"] == listing["skills"][0]

    first = await client.read_resource(URI)
    assert "blue release" in first.contents[0].text
    source.text = "Deploy the green release using the deployment tool."
    second = await client.read_resource(URI)
    assert "green release" in second.contents[0].text
```

The registration is a snapshot of declarations, not of callback results.
`read_skill` runs on each read and sees the current injected source. Supporting
files can use additional `extension.resource()` callbacks. Handlers may also
receive `Exchange` or an authenticated `Principal` through dependency injection.
Apply the same access rules to listings, skill details, and file reads.

Install this extension instead of `Skills.from_directory()` in the same
registry. Both use `io.modelcontextprotocol/skills`, and duplicate extension
identifiers are rejected. To combine static and dynamic skills, serve both
through one set of listing, detail, and resource handlers.

The models in `aiohttp_tiny_mcp.skills` currently describe static manifests.
For `resources: "dynamic"`, use your own `CacheableResult` subclasses as above,
or dictionaries that include `ttlMs` and `cacheScope`.

## Older clients

The same callback is available at
`mcp-extensions://io.modelcontextprotocol/skills/deploy/SKILL.md` on older
revisions. Find it through `resources/list` or the extension's `manifest.json`.
Continue the example to verify that the legacy resource also reads current data:

<!-- name: async test_dynamic_skills -->
```python
legacy_uri = "mcp-extensions://io.modelcontextprotocol/skills/deploy/SKILL.md"

async with connect(registry, adapter="2025-11-25") as client:
    assert legacy_uri in {resource.uri for resource in await client.list_resources()}
    source.text = "Check the green release with the health tool."
    document = await client.read_resource(legacy_uri)
    assert "health tool" in document.contents[0].text
```

See [List skills on older MCP revisions](extensions.md#list-skills-on-older-mcp-revisions)
for pagination and manifest examples. Older clients receive ordinary resources;
read `mcp-extensions://io.modelcontextprotocol/skills/skills/list` to receive the
current entries, including `resources: "dynamic"`. This read runs the same handler
as native `skills/list`. In the viewer, select that URI under **Resources**
and click **Read**. Older revisions do not have native extension views.

## Changing the catalog

The example changes content at an existing URI. Changing which skills exist
requires additional routing and discovery work:

- Custom `skills/list` and `skills/get` handlers can query a live catalog.
- `Extension.resource()` accepts fixed URIs and templates such as
  `skill://{name}/SKILL.md`. Register templates to route files from a live catalog.
- `Registry.resource()` supports templates, but resources registered there do
  not acquire an extension's legacy prefix automatically. Their templates are
  listed through `resources/templates/list`; concrete instances are not enumerated.
- The legacy extension manifest captures route declarations at installation.
  Its `methodResources` points to live handlers; it does not snapshot
  their catalogue results.

There is no built-in async Skills provider with `list`, `get`, and `read` yet.
For an unbounded live catalog, register list/get handlers with
`extension.method()` and file templates with `extension.resource()`. The method
resources are generated automatically. For a finite catalog, register file URIs at
startup and let their handlers return current content.
