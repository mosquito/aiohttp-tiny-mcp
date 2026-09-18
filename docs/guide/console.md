# The console

A page for trying a server out: connect, see what it offers, fill in a call,
and watch every message go over the wire. Mount it and it is there; leave it
out and nothing changes.

<!-- name: async test_console; fixtures: serve -->
```python
from pydantic import BaseModel

from aiohttp_tiny_mcp import Endpoint, Exchange, Registry, elicit
from aiohttp_tiny_mcp.console import Console

registry = Registry("console-demo", "1.0")


class Deploy(BaseModel):
    service: str


@registry.tool
async def deploy(args: Deploy, ex: Exchange) -> str:
    """Deploy a service, once somebody agrees to it."""
    agreed = await ex.ask("confirm", elicit(f"Deploy {args.service}?"))
    return f"deployed {args.service}" if agreed.accepted else "stopped"


app = Endpoint(registry).app("/mcp")
Console(
    "/mcp",
    title="Deployments",
    description="Ship a service, once somebody agrees to it.",
).setup(app, "/console")
```

That is the whole of it. `Console("/mcp")` says where the endpoint is;
`setup(app, "/console")` says where the page goes. `routes("/console")` returns
the same three route definitions for `add_routes`, where an application
registers its own routes in one place. The title and the description are what a
person sees on opening it, and they are the deployment's own words -- a console
with no title says nothing about which server it reached.

The page prints absolute addresses for its stylesheet and script, and takes
them from the request that asked for the page. A prefix it was never told
about, from `add_subapp`, is therefore still correct.

## Nothing to configure

The page is served by the server it talks to, so it is told where the endpoint
is rather than asking, and connects on opening. Its requests are therefore
same-origin, and no origin has to be allowed:

<!-- name: async test_console -->
```python
from aiohttp.test_utils import TestClient, TestServer

async with TestClient(TestServer(app)) as client:
    page = await client.get("/console")
    assert 'data-endpoint="/mcp"' in await page.text()

    # The same request a browser on that page makes.
    called = await client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {}},
        headers={
            "MCP-Protocol-Version": "2025-11-25",
            "Origin": str(client.make_url("")).rstrip("/"),
        },
    )
    assert called.status == 200
```

A page from anywhere else is still refused. That is the DNS-rebinding
protection the transport asks for, and it is not weakened by this: a rebound
page keeps the attacker's origin while reaching your host, so the two
disagree. See [Transports](../deployment/transports.md#origin-checking).

## What it shows

**The bar** shows where it is connected, which revision it is speaking, and
whether it is willing to be asked. Connect and Disconnect are the same button.

**Offered** lists the tools, resources, resource templates and prompts, with
what each says about itself. A tool that declared `destructiveHint` is marked.
On `2026-07-28`, it also lists declared extensions and skills. Listings follow
pagination cursors, including skill catalogs.

**The middle** renders the description as the markdown it is -- headings,
lists, code spans and blocks, links -- and builds a form from the selected
thing's schema -- types,
defaults, ranges, enumerations and which fields are required -- and shows what
came back: text, structured content, progress and log messages.

**Traffic** is every JSON-RPC message either way, in order, expandable. On a
narrow screen it starts folded, with a dot when something has arrived: it is
the panel opened when something is wrong, not the one worked in. **Offered**
folds there too, once something has been chosen from it -- which is the moment
it has done its job. This is
the part worth having when something is not behaving: it shows what was sent
rather than what you meant to send.

## Descriptions are markdown

A description is prose, and prose is markdown whether or not anybody said so,
so the page renders it: paragraphs reflowed to the panel, `code` as code,
lists as lists, fenced blocks kept as written.

It renders them itself, in about a hundred lines. A library would have meant a
fourth file, a version to keep, and either a network the console cannot assume
or a script from somewhere else on a page that holds the whole exchange.

Everything from the server is escaped before a single tag is added, and links
are limited to `http`, `https` and `mailto`. A description cannot bring its own
HTML, which matters most exactly when the server is one you have not read.

## Every revision

The revision chooser is not a display: the page speaks the one it names, and
the five differ in how they describe a server, whether they open a session,
and how a question reaches the client. Disconnect, choose another, connect --
the same server, seen the way that revision sees it.

That is the console's real use. A tool that behaves on `2026-07-28` and not on
`2025-03-26` shows it here in two clicks, with the traffic beside it.

## Extensions and skills

Select an extension to inspect its capability settings and send a custom
method with JSON parameters. The console supplies protocol metadata and headers;
the extension documentation defines method names and argument shapes.

When discovery declares `io.modelcontextprotocol/skills`, the console lists
skills by name and URI. Select one and click **Inspect** to fetch its current
frontmatter and file manifest through `skills/get`. Click a file to read it;
the console checks its byte size and SHA-256 digest before displaying content.
Digest verification requires HTTPS or localhost. Scripts are displayed as
content and are never executed.

The viewer does not activate skills in an agent. For a dynamic manifest,
click the instruction URI to read its current content; there are no published
digests to verify. A failed skill listing leaves the rest of the catalog available.

On older revisions, the console shows ordinary **Resources** and **Resource templates**.
There is no **Extensions** or **Skills** group. Compatibility routes use the
`mcp-extensions://{name}/...` namespace and the standard resource API.

To list skills on `2025-11-25` or another older revision:

1. Under **Resources**, select
   `mcp-extensions://io.modelcontextprotocol/skills/skills/list` and click **Read**.
2. Inspect the JSON catalogue. Click a file URI under **Read a resource** to open it.
3. For skill details, select the resource template
   `mcp-extensions://io.modelcontextprotocol/skills/skills/get?params={params}`.
   Enter `{"uri":"<skill-uri>"}` in `params` and click **Read**.
   The viewer percent-encodes the JSON for you.
4. If the listing returns `nextCursor`, select the `skills/list?params={params}`
   template and enter `{"cursor":"<nextCursor>"}` to read the next page.

The viewer does not call native `skills/list` or `skills/get` on older revisions.
Every registered method has a resource route automatically. Custom handlers
use `extension.method()`; their files belong to `extension.resource()`.
Install the extension after declaring its methods and files. Restart the server
and reload the viewer after changing declarations, including with an editable install.

The manifest remains an ordinary JSON resource that describes the available
routes. It does not declare protocol extension support. See
[Extension compatibility through resources](extension-compatibility.md) and
[Dynamic skills](dynamic-skills.md).

## Questions

A handler that calls `ex.ask` puts a question to the page, and the page puts it
to the person -- with a form built from the request's schema, and the three
answers the protocol defines: accept, decline, cancel.

Untick **Can be asked** and the console stops declaring the capability, which
is how to see what a client that cannot answer gets. A handler with a default
takes it; one without is told the client cannot be asked.

## Your own page

Pass `template` to serve your own HTML, and `variables` to fill in whatever it
carries. Every `{{NAME}}` is replaced, and the values are escaped.

<!-- name: async test_console_template -->
```python
from pathlib import Path
from tempfile import TemporaryDirectory

from aiohttp.test_utils import TestClient, TestServer

from aiohttp_tiny_mcp import Endpoint, Registry
from aiohttp_tiny_mcp.console import Console

registry = Registry("console-demo", "1.0")

with TemporaryDirectory() as into:
    template = Path(into) / "console.html"
    template.write_text(
        '<html data-endpoint="{{ENDPOINT}}">'
        '<link rel="stylesheet" href="{{BASE}}/console.css">'
        "<h1>{{TITLE}}</h1><p>Ask {{SUPPORT}}</p>"
        '<script src="{{BASE}}/console.js"></script></html>'
    )

    app = Endpoint(registry).app("/mcp")
    Console(
        "/mcp",
        title="Deployments",
        template=template,
        variables={"SUPPORT": "ops@example.com"},
    ).setup(app, "/console")

    async with TestClient(TestServer(app)) as client:
        page = await (await client.get("/console")).text()

assert "ops@example.com" in page
assert "{{" not in page
```

The stylesheet and the script are still the package's. Keep the element ids the
bundled page uses, or the script has nothing to fill in.

## Before you mount it

It authenticates nobody. A console reaches every tool the server offers, so put
it behind whatever the rest of the deployment is behind, or leave it out where
that is not true. See [Authentication](auth.md) for endpoint protection.

## Three files, no build

One HTML, one CSS, one JavaScript, served from the package. No bundler, no
dependencies, no step between editing and reloading.

It speaks `2026-07-28` today. What differs between revisions is stated once at
the top of `console.js`, in the same shape the Python adapters use, which is
where the others will go.
