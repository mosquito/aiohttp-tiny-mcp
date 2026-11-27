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
the same asset route definitions for `add_routes`, where an application
registers its own routes in one place. The title and the description are what a
person sees on opening it, and they are the deployment's own words -- a console
with no title says nothing about which server it reached.

The page prints absolute addresses for its stylesheet and script, and takes
them from the request that asked for the page. A prefix it was never told
about, from `add_subapp`, is therefore still correct.

## Nothing to configure

The server supplies the initial endpoint URL. The console restores the last
endpoint from `localStorage` and waits for **Connect** on first open. Reloading
a connected console reconnects to its endpoint with the same revision and
checkbox settings. This connection state is stored in `sessionStorage` for the
current tab and console path. **Disconnect**, clearing credentials, or a failed
connection removes it. Each reconnect starts a new MCP session; it does not
replay tool calls. Requests stay on the same origin:

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
        ConsoleEndpoint(
            "/team/mcp", "Tools for one team", TeamQuery, auth=("none",), server_name="team_tools"
        ),
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
Each record belongs to one console path and endpoint origin and path. Query
parameters do not affect credential selection, so changing them preserves
authentication. Different origins and paths use separate records. Reloading
restores the saved record; switching endpoints does not delete it. **Clear**
removes the saved credentials for the current endpoint path.
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

## Add an agent

Click **Add agent** to generate configuration for the current endpoint URL.
The dialog works while disconnected and does not make an MCP request. Choose
a server name, an **Agent**, and a **Configuration level**, then copy
the result into the file shown in the dialog. **Project** applies to the current
project; **User** applies across your projects. Merge the server entry with
existing settings.

Set `ConsoleEndpoint(server_name="team_tools", ...)` to supply the initial server
name for that endpoint. Names accept 1–64 letters, numbers, underscores, or hyphens.
Without a declared name, the console derives one from its title. Names entered
manually remain available for that endpoint until the page reloads.

Not every agent harness can discover or read native MCP resources, even when
it can connect and call tools. Resource support can depend on the agent version
or installed plugins. Under **Resources**, select the interface your agent can use:

- **as tools** exposes resource listing and reading through ordinary MCP tools.
  Use it when the agent cannot access native resources.
- **native** uses the MCP resource interface. Choose it when the agent supports
  the resource features you need.
- Select both to expose both interfaces, for example when you need tool access
  and native resource subscriptions. Tool mode alone does not provide subscriptions.

See [Access through tools](resources.md#access-through-tools) for URL examples
and the available resource tools.

Changing the agent applies that agent's defaults. Changing the name,
configuration level, or header selection preserves your resource choices.
At least one resource interface must
remain selected before you can copy the configuration.

The default level is **User**. The console remembers your level choice in
`localStorage` for each console path, including after reopening or reloading.
Missing, invalid, or unavailable storage uses **User**. The file path depends
on the selected agent. These formats were checked against their official
references on 2026-11-16:

| Agent | Project file | User file | HTTP configuration |
| --- | --- | --- | --- |
| [Claude Code](https://code.claude.com/docs/en/mcp#user-scope) | `.mcp.json` | `~/.claude.json` | `mcpServers`, `type: "http"`, `url` |
| [Codex](https://learn.chatgpt.com/docs/config-file/config-basic) | `.codex/config.toml` | `~/.codex/config.toml` | `mcp_servers`, `url`, `http_headers` |
| [Cursor](https://cursor.com/docs/mcp) | `.cursor/mcp.json` | `~/.cursor/mcp.json` | `mcpServers`, `url` |
| [GitHub Copilot](https://code.visualstudio.com/docs/agents/reference/mcp-configuration) | `.mcp.json` | `~/.copilot/mcp-config.json` | `mcpServers`, `type: "http"`, `url`, `tools` |
| [Gemini CLI](https://geminicli.com/docs/tools/mcp-server/) | `.gemini/settings.json` | `~/.gemini/settings.json` | `mcpServers`, `httpUrl` |
| [Pi](https://docs.pi.dev/latest/mcp) | `.pi/mcp.json` | `~/.pi/agent/mcp.json` | `mcpServers`, `url` |
| [Antigravity](https://antigravity.google/docs/mcp) | `.agents/mcp_config.json` | `~/.gemini/config/mcp_config.json` | `mcpServers`, `serverUrl` |
| [OpenCode V2](https://opencode.ai/docs/config/#locations) | `opencode.json` | `~/.config/opencode/opencode.json` | `mcp.servers`, `type: "remote"`, `url` |

Paths use each agent's default configuration directory. If you override that
directory, use the corresponding file there; for example, Copilot uses
`$COPILOT_HOME/mcp-config.json` when `COPILOT_HOME` is set. Codex and Pi require
project trust before reading project configuration. Claude's `~/.claude.json`
contains other settings: merge the generated top-level `mcpServers` entry and
preserve those settings, including any `projects` entries.

The project-level `.mcp.json` file supports both Claude Code and Copilot;
it is not exclusive to Claude Code. Their user-level files differ, as shown above.
Other formats are not interchangeable. For example, Gemini's `url` field selects
legacy SSE, while `httpUrl` selects Streamable HTTP. Current VS Code Copilot
uses the portable `.mcp.json` format; its older `.vscode/mcp.json` format uses
`servers` and is intended for Local harness compatibility.

The generated URL includes the current query parameters. Saved authentication
headers are included only when their endpoint origin and path match. Clear
**Use saved auth** to generate a URL-only configuration
and use the agent's own sign-in flow where supported. Query tokens remain part
of the URL. The console does not copy its OAuth client registration to agents.

Pi, Antigravity, and Copilot configurations default to `resources=tool` for compatibility.
Other agents default to native resources without a `resources` query parameter.
Selecting both checkboxes generates
`resources=tool&resources=native`. These choices replace existing `resources`
values and preserve other query parameters. These presets do not imply that
every version of an agent lacks native resource support.
Selecting only native resources removes all existing `resources` parameters.
The Antigravity format and paths were checked on 2026-11-21.

Codex defaults to native resources with **include prompts** enabled. This adds
`prompts=resource` to the generated URL. Select or clear **include prompts**
when using native resources to control this setting. The resource tools already
include [prompt addresses](prompts.md#clients-without-native-prompts), including
in the Pi preset.

When the registry disables resource tools, pass the same setting to the console:

```python
from aiohttp_tiny_mcp import Registry
from aiohttp_tiny_mcp.console import Console

registry = Registry("example", "1.0", resource_tools=False)
console = Console(resource_tools=registry.resource_tools)
```

With `Console(resource_tools=False)`, the resource checkboxes are hidden. All generated
configurations preserve the endpoint URL without adding or replacing `resources` values.

**Copy configuration** copies the selected format. If clipboard access is
unavailable, the dialog selects the text for keyboard copying. Each format
includes a configuration path, verification instructions, and its documentation
link. Closing the dialog leaves the console URL and connection unchanged.

## Descriptions are markdown

A description is prose, and prose is markdown whether or not anybody said so,
so the page renders it: paragraphs reflowed to the panel, `code` as code,
lists as lists, fenced blocks kept as written.

Headings keep their levels and use distinct sizes and spacing. Use `#` through
`######` for levels 1–6. The console also accepts `#######` for level 7 and
standalone, attribute-free `<h1>Title</h1>` through `<h7>Title</h7>` lines.
Level 7 renders with `role="heading"` and `aria-level="7"`. Heading contents
support inline Markdown; embedded HTML and tags with attributes stay escaped.

The Markdown renderer is included with the console. It needs no external
library or network request.

The renderer escapes server text. Links can start with `http:`, `https:`,
`mailto:`, `#`, or `/`.

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

The catalogue and server title display declared icons on revisions that carry
them. The console selects light/dark variants and scales images to 16 pixels.
See [Icons](icons.md) for presets, custom images and the runnable demo.

A handler that calls `ex.ask` puts a question to the page, and the page puts it
to the person -- with a form built from the request's schema, and the three
answers the protocol defines: accept, decline, cancel.

Untick **Can be asked** and the console stops declaring the capability, which
is how to see what a client that cannot answer gets. A handler with a default
takes it; one without is told the client cannot be asked.

Select **Can answer for a model** before connecting to answer
model completion requests yourself. The dialog shows the messages and model
options, and accepts a text completion. The reply names its model
`console-operator`; no API key or model service is needed. **Decline**,
**Cancel**, and Escape return an error rather than an empty completion.
For a pushed request, the server waits up to its configured completion timeout.
With MRTR, the client controls when it submits the completion. Background notifications
continue while the dialog is open. Disconnect before changing the checkbox.

To try it, run `uv run python examples/demo.py`, open
`http://127.0.0.1:8080/console`, select the checkbox, and connect. Choose
**summarize**, enter text, and click **Call**. Enter a sentence in the dialog
and click **Send completion**. Try **Decline**, then disconnect and clear the
checkbox to see the missing-capability refusal. This works on all five revisions.

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
