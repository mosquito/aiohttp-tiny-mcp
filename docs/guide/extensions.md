# Extensions and skills

Use `Extension` to bundle custom request methods, capability settings, and
resources. Install it with `registry.extension(extension)` before serving requests.
The same registration works over Streamable HTTP and stdio.

On MCP `2026-07-28`, `server/discover` advertises the extension and its methods
are callable. Older revisions expose its files and a manifest through resources
under `mcp-extensions://{name}/...`, where `{name}` is the full extension identifier.
Every registered method also has a resource route, including methods that change state. Older clients
call `resources/read`; they do not send custom JSON-RPC method names.

See [Extension compatibility through resources](extension-compatibility.md) for
the protocol boundary, wire examples, invocation rules, and client limitations.

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

Protocol metadata is available through `Exchange`; `_meta` is removed before
validating handler arguments. Method fields, including `inputResponses` and
`requestState`, remain available to the argument model. HTTP authentication, origin
checks, and required protocol headers still apply. Apply any method-specific
authorization in the handler using an injected `Principal`.

## Bundle resources for older clients

Register fixed resources or URI templates with `extension.resource()`. The default legacy path
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
    prefix = "mcp-extensions://example.org/manual/"
    resource = await client.read_resource(prefix + "start.md")
    assert resource.contents[0].text.startswith("# Start")
    manifest = await client.read_resource(prefix + "manifest.json")
    assert json.loads(manifest.contents[0].text)["resources"] == [prefix + "start.md"]
```

`resources/list` includes the manifest and files on all four older revisions.
The manifest lists the extension identifier, settings, method names, and legacy
resource URIs. `resourceTemplates` lists templates, and `methodResources` maps
all methods to resource routes and parameter schemas. On `2026-07-28`,
resources use their declared URIs; legacy manifests and method routes are hidden.
`manifest.json` is reserved within the legacy prefix.

## Invoke methods on older revisions

Every `extension.method()` registration automatically gets a resource route.
The same handler runs on each invocation, with argument validation and dependency
injection. No compatibility flag, separate server resource, or copied catalogue
is needed. The manifest includes the parameter schema and any declared output model.

<!-- name: async test_extension_method_resource -->
```python
import json
from urllib.parse import quote

from pydantic import BaseModel
from aiohttp_tiny_mcp import Extension, Registry
from aiohttp_tiny_mcp.testing import connect


class Lookup(BaseModel):
    name: str


extension = Extension("example.org/catalog")


@extension.method("catalog/lookup")
async def lookup(args: Lookup) -> dict:
    return {"entry": {"name": args.name}}


registry = Registry("catalog", "1")
registry.extension(extension)

async with connect(registry, adapter="2025-11-25") as client:
    params = quote(json.dumps({"name": "deploy"}), safe="")
    uri = "mcp-extensions://example.org/catalog/catalog/lookup?params=" + params
    result = await client.read_resource(uri)
    assert json.loads(result.contents[0].text)["entry"]["name"] == "deploy"
```

`resources/templates/list` advertises the parameterized route. Methods without
required parameters also have a fixed URI without a query in `resources/list`.
The extension manifest describes both through `methodResources`.

URI fields in arguments and results are translated for resources registered
on the same extension, including templates. Cursors remain opaque. URI template
variables retain their encoded values; decode them in the file handler when needed.

The mapping includes state-changing methods such as `tasks/update` and
`tasks/cancel`. Reading their method resources invokes those operations. The
resource descriptions and manifest state this behavior; listings do not invoke
handlers. Clients must not treat method resources as static files or reuse a
cached read when they intend to invoke the method again.

Results remain JSON objects in resource contents. Custom result discriminators
such as `resultType: "task"` are preserved. `Rejected` errors keep their code,
message, and data as errors from `resources/read`. The mapping does not add
custom JSON-RPC methods or extension capabilities to an older MCP revision.

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

registry = Registry("deployment", "1.0", page_size=1)
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
`mcp-extensions://io.modelcontextprotocol/skills/deploy/SKILL.md`.
They receive ordinary resources; automatic skill loading depends on the client.
The loader does not advertise the optional `resources/directory/read` method.

Serving a script publishes its contents. It never executes it. Keep server-side
actions in existing MCP tools and let the client decide how to load instructions.

## List skills on older MCP revisions

`Skills.from_directory()` exposes `skills/list` and `skills/get` through resources
on all four older revisions. Complete initialization, then read:

```text
mcp-extensions://io.modelcontextprotocol/skills/skills/list
```

The JSON content contains `skills` and an optional `nextCursor`. Each skill has
frontmatter and a file manifest. File URIs already use the extension namespace.
For pagination or `skills/get`, append `?params=` followed by percent-encoded JSON.
Continue the directory example above:

<!-- name: async test_extension_skills -->
```python
import json
from urllib.parse import quote

prefix = "mcp-extensions://io.modelcontextprotocol/skills/"

for revision in ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25"):
    async with connect(registry, adapter=revision) as client:
        entries = []
        params = {}
        while True:
            uri = prefix + "skills/list"
            if params:
                uri += "?params=" + quote(json.dumps(params), safe="")
            page = json.loads((await client.read_resource(uri)).contents[0].text)
            entries.extend(page["skills"])
            if not page.get("nextCursor"):
                break
            params = {"cursor": page["nextCursor"]}

        skill_uri = entries[0]["uri"]
        assert skill_uri == prefix + "deploy/SKILL.md"
        params = quote(json.dumps({"uri": skill_uri}), safe="")
        detail = await client.read_resource(prefix + "skills/get?params=" + params)
        assert json.loads(detail.contents[0].text)["skill"] == entries[0]
        document = await client.read_resource(skill_uri)
        assert "name: deploy" in document.contents[0].text
```

In the [console](console.md#extensions-and-skills), select the namespaced
`skills/list` URI under **Resources** and click **Read**. Parameterized routes
appear under **Resource templates**. Older revisions have no **Extensions** or
**Skills** group; these are ordinary MCP resource operations.

This is a library convention carried by the standard resource API. It does not
add native extension support to older MCP protocols or agent clients.

## Dynamic skills

`Skills.from_directory()` reads files once. For instructions generated on each
request, use async `Extension` handlers instead. See
[Dynamic skills](dynamic-skills.md) for a complete example, cache settings,
and the limits of changing the catalog at runtime.

## What the generic API provides

`Extension` provides capability advertisement, method dispatch, Pydantic argument
validation, dependency injection, and resource registration. Every registered
method has an automatic resource representation, including state-changing methods. On older revisions, the viewer displays
the mapped resources and templates without interpreting them as extensions.

Registering an identifier does not implement that extension's behavior:

| Extension | Additional work required |
| --- | --- |
| [Tasks](https://modelcontextprotocol.io/extensions/tasks/overview) | Durable jobs, capability checks, polling, input handling, and cancellation. Custom method results preserve `resultType: "task"`. The core tool path still coerces returns to `CallToolResult`; returning Tasks from `tools/call` needs additional integration. |
| [Apps](https://modelcontextprotocol.io/extensions/apps/overview) | HTML resources, tool UI metadata, CSP, and a host bridge. The viewer does not implement an Apps iframe host; the tool decorator has no `_meta` registration option. |
| [OAuth Client Credentials](https://modelcontextprotocol.io/extensions/auth/oauth-client-credentials) | An authorization server, client registration, token acquisition/renewal, and token validation. The library's HTTP authorization hooks do not implement the grant. |
| [Enterprise-Managed Authorization](https://modelcontextprotocol.io/extensions/auth/enterprise-managed-authorization) | IdP trust, ID-JAG issuance/exchange, identity mapping, and policy enforcement. Capability declarations do not perform those flows. |

Use `Exchange` to inspect client capabilities and request metadata. Method
argument models can declare `inputResponses` and other extension-specific fields. The viewer does not declare Tasks, Apps, or
enterprise authentication support merely because the server advertises them.
