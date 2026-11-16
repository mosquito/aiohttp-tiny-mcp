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

The server supplies the initial endpoint URL. The console restores the last
endpoint from `localStorage` and waits for **Connect**. Opening or reloading the
page does not start a connection. Requests stay on the same origin:

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
whether it is willing to be asked. Click the server title to return to its
instructions. The title is enabled when instructions are available. Connect and Disconnect are the same button.
**Notifications** is checked by default. It opens a background notification
stream after connecting. Disconnect before changing this setting.
The console listens for advertised list changes, extension broadcasts, and
updates to listed resources. Reading a resource also subscribes to its concrete
URI, including a URI filled from a resource template. Events appear in **Traffic**
and the result panel. A notification reports a change; it does not reload the
resource content. Use **Read** or **Refresh** to fetch the current data.
The stream closes on disconnect and retries temporary connection failures.
MCP `2026-07-28` uses `subscriptions/listen`; older revisions use resource
subscriptions and a GET stream. A new stateless subscription does not replay
events missed while disconnected.

**Endpoint** always remains a text field. It starts with the configured path and
accepts a query string, such as `/mcp?mcp=2025-06-18` to pin a revision. You can
enter additional query parameters even when endpoint declarations are present.

**Offered** lists the tools, resources, resource templates and prompts, with
what each says about itself. A tool that declared `destructiveHint` is marked.
On `2026-07-28`, it also lists declared extensions and skills. Listings follow
pagination cursors, including skill catalogs. Selecting a tool saves its name
in the page URL as `#toolName`. Reloading selects it again if the server still
offers it. Tool forms are saved as you edit them, in separate `localStorage`
entries for each console path, endpoint, and tool. Returning to a tool or reloading
restores its inputs, checkbox selections, and `null` state.

**The middle** renders the description as the markdown it is -- headings,
lists, code spans and blocks, links -- and builds a form from the selected
thing's schema -- types,
defaults, ranges, enumerations and which fields are required -- and shows what
came back: text, structured content, progress and log messages.

Fields with `enum` or fixed `oneOf` choices use a select control. Each `oneOf`
branch must define `const` or a single-value `enum`; its `title` labels the option.
The selected value keeps its JSON type. Other `oneOf` schemas use the existing
field renderer.

Nullable fields show a `null` checkbox beside the heading. Checking it disables
the input and sends JSON `null`. Unchecking it restores the previous input.
A `null` default checks the box automatically, including in simplified schemas.
Arrays with fixed `items.enum` or `items.oneOf` choices use checkbox groups.
Checked options form the submitted array; no checked options sends `[]`.

**Traffic** is every JSON-RPC message either way, in order, expandable. On a
narrow screen it starts folded, with a dot when something has arrived: it is
the panel opened when something is wrong, not the one worked in. **Offered**
folds there too, once something has been chosen from it -- which is the moment
it has done its job. This is
the part worth having when something is not behaving: it shows what was sent
rather than what you meant to send.

## Declared endpoints

Pass `endpoints` to add an endpoint selector, a short description, and query
parameter fields inside the existing **Authentication** dialog. The main URL
field remains editable. Each `ConsoleEndpoint` describes an existing route;
declarations do not register routes or validate incoming requests. The application
still owns query validation and authorization.

Use a Pydantic model to declare query parameter names, descriptions, defaults,
required fields, and choices:

<!-- name: async test_console_declared_endpoints -->
```python
from typing import Literal

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from pydantic import BaseModel, Field

from aiohttp_tiny_mcp import Endpoint, Registry
from aiohttp_tiny_mcp.console import Console, ConsoleEndpoint


class TeamQuery(BaseModel):
    team: str = Field(description="Team name")
    environment: Literal["staging", "production"] = "staging"
    labels: list[str] = Field(default_factory=list, description="Filter by labels")


application = web.Application()
application.add_routes(Endpoint(Registry("team-tools", "1.0")).routes("/team/mcp", name="team"))
application.add_routes(
    Endpoint(Registry("shared-tools", "1.0")).routes("/shared/mcp", name="shared")
)
Console(
    title="Operations",
    endpoints=[
        ConsoleEndpoint("/team/mcp", "Tools for one team", TeamQuery, auth=("none",)),
        ConsoleEndpoint("/shared/mcp", "Shared read-only tools", auth=("none",)),
    ],
).setup(application)

async with TestClient(TestServer(application)) as client:
    response = await client.get("/console")
    assert response.status == 200
    assert 'data-endpoint="/team/mcp"' in await response.text()
```

The first declaration supplies the initial URL and takes precedence over
`endpoint_path`. An empty list adds no endpoint form. Declared paths must start with
`/` and stay on the same origin; fragments and duplicate paths are rejected.
A path can contain fixed query parameters, such as `/team/mcp?mode=readonly`.

Open **Authentication** to select an endpoint and edit its query fields. Choose
**None** for anonymous access or **Query parameters** for credentials passed in
query fields. Neither adds an Authorization header. **Connect** applies the form
values to the URL field and connects. **Cancel** leaves the URL and connection
unchanged. Manual URLs can include parameters that are absent from the declaration.

The optional `auth` list declares the endpoint's supported authentication types:
`none`, `basic`, `bearer`, `query`, `custom`, and `oauth`. The dialog offers only
the declared types. `auth=None` preserves the default authentication choices;
an explicit list must be nonempty and contain no duplicates. Declaring `oauth`
also requires `Console(oauth_client_id=...)`. These declarations configure the
console, not the server's authentication policy.

Authentication values are saved in browser `localStorage` when you connect.
Saved values include usernames, passwords, tokens, and custom header names.
Each record belongs to one console path and exact endpoint URL, including its
query string. Reloading can restore that record; switching endpoints does not
delete it. **Clear** removes the saved credentials for the current endpoint.
Closing or cancelling the dialog does not save edits. If browser storage is
unavailable, credentials remain usable for the current page only.

Query models must contain named scalar fields or lists of scalars. Strings,
numbers, integers, booleans, enums, and nullable fields are supported. Nested
objects, recursive models, and unions of different non-null types are rejected.
Field aliases become query parameter names.

The console encodes values with `URLSearchParams`. Lists use repeated keys,
such as `labels=red&labels=blue`; booleans use `true` or `false`. Empty fields
and null values are omitted. Declared fields replace fixed query values with
the same name. Fill required fields before submitting the dialog.

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

The console is an HTTP client. Its authentication dialog supports anonymous
access, query credentials, Basic, Bearer tokens, custom headers, and configured
OAuth sign-in. See the [authentication guide](auth.md#console-authentication)
for credential handling. `Registry(auth=...)` leaves console assets public.
Optional application middleware can use `Console.is_public(request)` to identify them.

## Static files, no build

The console serves HTML, CSS, JavaScript, and an OAuth callback page from the
package. No bundler, no dependencies, no step between editing and reloading.

It supports every library protocol revision. Differences between revisions are
defined in `REVISIONS` at the top of `console.js`.
