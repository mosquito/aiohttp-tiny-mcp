# Extensions and skills

Use `Extension` to bundle custom request methods, capability settings, and
resources. Install it with `registry.extension(extension)` before serving requests.
The same registration works over Streamable HTTP and stdio.

On MCP `2026-07-28`, `server/discover` advertises the extension and its methods
are callable. Older revisions expose its files and a manifest through resources
under `mcp-extensions/{name}/...`, where `{name}` is the full extension identifier.
This resource convention does not add extension methods to older protocols.

## Declare an extension

Handlers receive validated Pydantic arguments and can request dependencies,
including `Exchange`, as tools do. Return a result object, not a tool result.

<!-- name: async test_extension -->
```python
from pydantic import BaseModel

from aiohttp_tiny_mcp import Extension, Registry
from aiohttp_tiny_mcp.testing import connect


class Lookup(BaseModel):
    name: str


extension = Extension("example.org/catalog", capabilities={"lookup": True})


@extension.method("catalog/lookup")
async def lookup(args: Lookup) -> dict:
    return {"entry": {"name": args.name}}


registry = Registry("catalog", "1.0")
registry.extension(extension)

async with connect(registry, adapter="2026-07-28") as client:
    discovery = await client.initialize()
    assert discovery["capabilities"]["extensions"]["example.org/catalog"] == {"lookup": True}
    result = await client.request_method("catalog/lookup", {"name": "deploy"})
    assert result["entry"] == {"name": "deploy"}
    assert result["resultType"] == "complete"
```

`extension.method("catalog/lookup", lookup)` is the equivalent direct call.
Register dependency providers before installing the extension. Installation
rejects duplicate identifiers, conflicting methods or resources, missing
providers, and attempts to replace base protocol methods. It checks all
declarations before changing the registry.

Configure the extension before installation. Each registry takes a snapshot
of its declarations, so one extension can be installed in several registries.
`min_revision` defaults to `2026-07-28`; later values hide its methods and
capability declaration from earlier revisions.

## Results and errors

Return a dictionary, a Pydantic model, or a subclass of
`aiohttp_tiny_mcp.models.ResultModel`. For cacheable methods, subclass
`CacheableResult`; the adapter supplies `ttlMs: 0` and `cacheScope: "private"`
unless the handler sets them. The adapter also supplies `resultType` and server
identity metadata.

Invalid handler arguments produce JSON-RPC error `-32602`. To report a specific
protocol failure, raise `Rejected(Failure(...))` from `aiohttp_tiny_mcp.core`.
Unexpected exceptions produce `-32603`. Extension methods are requests and require
an id; extension notification handlers are not supported.

Protocol metadata and retry fields are available through `Exchange`; they are
removed before validating the handler's arguments. HTTP authentication, origin
checks, and required protocol headers still apply. Apply any method-specific
authorization in the handler using an injected `Principal`.

## Bundle resources for older clients

Register fixed resources with `extension.resource()`. The default legacy path
is the part after `://`; `legacy_path=` overrides it.

<!-- name: async test_extension_resources -->
```python
import json

from pydantic import BaseModel

from aiohttp_tiny_mcp import Extension, Registry
from aiohttp_tiny_mcp.testing import connect


class Nothing(BaseModel):
    pass


extension = Extension("example.org/manual")


@extension.resource("manual://start.md", mime_type="text/markdown")
async def start(args: Nothing) -> str:
    return "# Start\nCall the existing deployment tool.\n"


registry = Registry("manual", "1.0")
registry.extension(extension)

async with connect(registry, adapter="2025-11-25") as client:
    prefix = "mcp-extensions/example.org/manual/"
    resource = await client.read_resource(prefix + "start.md")
    assert resource.contents[0].text.startswith("# Start")
    manifest = await client.read_resource(prefix + "manifest.json")
    assert json.loads(manifest.contents[0].text)["resources"] == [prefix + "start.md"]
```

`resources/list` includes the manifest and files on all four older revisions.
The manifest lists the extension identifier, settings, method names, and legacy
resource URIs. Method names are descriptive; callers cannot invoke those methods
on older revisions. On `2026-07-28`, resources use their declared URIs, and the
legacy manifest is hidden. `manifest.json` is reserved within the legacy prefix.

## Load skills from a directory

Install the optional YAML dependency:

```bash
uv add 'aiohttp-tiny-mcp[skills]'
```

Use a directory containing one skill or a tree of skills:

```text
skills/deploy/SKILL.md
skills/deploy/references/deployment.md
skills/deploy/scripts/deploy.py
```

Create the files, then pass their root to `Skills.from_directory()`. This example
uses pytest's `tmp_path`; an application can pass `"skills"` instead.

<!-- name: async test_extension_skills; fixtures: tmp_path -->
```python
from aiohttp_tiny_mcp import Registry
from aiohttp_tiny_mcp.skills import Skills
from aiohttp_tiny_mcp.testing import connect

directory = tmp_path / "skills" / "deploy"
directory.mkdir(parents=True)
(directory / "SKILL.md").write_text(
    "---\nname: deploy\ndescription: Deploy the application and verify its health.\n"
    "---\nUse the deployment tool, then check its health result.\n",
    encoding="utf-8",
)

registry = Registry("deployment", "1.0")
registry.extension(Skills.from_directory(tmp_path / "skills"))

async with connect(registry, adapter="2026-07-28") as client:
    discovered = await client.initialize()
    assert "io.modelcontextprotocol/skills" in discovered["capabilities"]["extensions"]
    listing = await client.request_method("skills/list")
    skill = listing["skills"][0]
    assert skill["uri"] == "skill://deploy/SKILL.md"
    detail = await client.request_method("skills/get", {"uri": skill["uri"]})
    assert detail["skill"] == skill
    content = await client.read_resource(skill["uri"])
    assert content.contents[0].text.startswith("---\n")
```

The loader implements the
[MCP Skills extension](https://github.com/modelcontextprotocol/ext-skills/blob/main/specification/stable/skills.mdx).
It preserves JSON-compatible frontmatter fields and publishes a complete file
manifest with SHA-256 digests and byte sizes. Nested skills have separate entries;
their files also belong to the enclosing skill's manifest. `page_size` controls
`skills/list` pagination and defaults to 100. Pass `nextCursor` back as `cursor`.

Files are read once at startup. Resource reads and digests use the same bytes,
including line endings. UTF-8 files use text contents; other files use base64
blob contents. Recreate the extension and registry to publish changed files.
The loader rejects symlinks, special files, invalid frontmatter, and skills
exceeding 512 files or 16 MiB. Only files inside discovered skill directories
are published, including hidden files.

Older clients read the same skill at
`mcp-extensions/io.modelcontextprotocol/skills/deploy/SKILL.md`.
They receive ordinary resources; automatic skill loading depends on the client.
The loader does not advertise the optional `resources/directory/read` method.

Serving a script publishes its contents. It never executes it. Keep server-side
actions in existing MCP tools and let the client decide how to load instructions.
