# What each revision gets

The claim this package makes is that an application declares something once and
a client loses a capability only where its revision genuinely cannot express it.
This page is that claim, stated precisely.

## The matrix

| | 2026-07-28 | 2025-11-25 | 2025-06-18 | 2025-03-26 | 2024-11-05 |
| --- | --- | --- | --- | --- | --- |
| Tools, resources, prompts, completion | yes | yes | yes | yes | yes |
| Structured output | any JSON value | object only | object only | as text | as text |
| Progress | yes | yes | yes | yes | without a message |
| **Ask the user** | MRTR | pushed request | pushed request | in the tool call | in the tool call |
| **Ask the client's model for a completion** | MRTR | pushed request | pushed request | pushed request | pushed request |
| **Logging** | per request | per session | per session | per session | per session |
| **Subscriptions** | `subscriptions/listen` | `resources/subscribe` + `GET` | same | same | same |
| **Sessions** | by handle | by `Mcp-Session-Id` | same | same | same |
| Tool hints (`annotations`) | yes | yes | yes | yes | no |
| Display titles | yes | yes | yes | no | no |
| Icons and `websiteUrl` | yes | yes | no | no | no |
| Cache hints on results | per resource and per method | no | no | no | no |
| Batching | no | no | no | yes | no |
| Header mirroring | yes | no | no | no | no |
| Extension methods and discovery | yes | resources only | resources only | resources only | resources only |
| Skill files | `skill://...` | `mcp-extensions://io.modelcontextprotocol/skills/...` | same | same | same |

Rows in bold are the ones where the mechanism differs but the application code
does not. Everything else is either present everywhere or a property of the
revision that no amount of work can add.

**Model completions.** `ex.completion()` asks the client's model for a completion on every
revision: pushed on the call's stream where there is one, and carried in the
result on `2026-07-28`, which restarts the handler with the answer. The adapter
chooses the channel; the handler uses the same API. A client that declared no
`sampling` wire capability is refused with the capability it needs.

## What is genuinely lost

The following differences affect what an older client receives.

**Structured output.** A tool returning a bare list gets `structuredContent` on
`2026-07-28` and text on `2025-11-25` and `2025-06-18`, which require an object
there. `2025-03-26` and `2024-11-05` predate the field entirely and get the
same data as text, which is where a tool of those revisions is supposed to put
it. The console reads it back either way, so it looks the same to a person.

**Legacy schemas.** Adapters simplify tool schemas for older clients and omit
tools whose input schema they cannot project. Set `min_revision` to limit a
tool to newer revisions.

**Icons.** A tool, a resource, a resource template, a prompt and the server info
carry `icons` on `2026-07-28` and `2025-11-25`, and the server info carries
`websiteUrl` with them. The three older revisions have no field for either, so the
adapter drops them. A resource link has no icon here yet. The bundled console
shows server and catalogue icons and selects the variant for its colour theme.

**Cache hints.** `ttlMs` and `cacheScope` exist only on `2026-07-28`. A resource
declares them with `cache_ttl` and `cache_scope`, and
`Registry(cache_hints={method: CacheHint(...)})` states a default per cacheable
method. The fallback is `ttlMs: 0` with `cacheScope: "private"`. See
[Cache hints](../guide/caching.md) for examples and precedence.

**Extension methods.** Older clients can list and read extension files and a
manifest under `mcp-extensions://{name}/...`. Every registered method, including methods that change state,
runs through a `resources/read` route. Native extension capabilities and custom
JSON-RPC methods remain unavailable on these revisions. See [Extension compatibility through resources](../guide/extension-compatibility.md).

## Where this package is stricter

### Upgrading existing declarations

Registration now rejects invalid tool names and resource templates instead of
publishing declarations a client cannot use. Before upgrading:

- Give tools ASCII names of 1–128 letters, digits, `.`, `_` or `-`. Use
  `@registry.tool(name="search")` if the Python function has a Unicode name.
- Replace malformed, repeated or adjacent template variables with distinct
  `{name}` variables separated by literal text. RFC 6570 operators are not supported.
  A URI may have at most 8192 characters and a template at most 256 variables.
- For HTTP+SSE, move connections and transactions released by middleware into
  [handler-scoped providers](../guide/dependencies.md#an-async-generator). The
  POST now returns `202` before the MCP handler runs; its middleware scope has ended.
- Replace imports of `access_log.TOKEN_QUERY_FILTER` with
  `access_log.install_token_filter()`. This keeps one filter that covers the
  credential names declared by all configured policies.

The new completion API and icon presets are additions; they do not replace a
released sampling API or add roots support.

**Tool names.** The specification's rule is a character set and a length, and
this package refuses a name that breaks it at registration. The official SDK
logs a warning and registers the tool anyway. A server that a host would reject
therefore does not start here, which is deliberate: every comparable mistake in
a declaration also raises at registration. See
[Names and addresses](../concepts.md#names-and-addresses).

**Resource templates.** A simple `{name}` variable matches one path segment.
One `{+path}` variable can span path segments. Other RFC 6570 operators,
repeated variables, and adjacent variables are refused at registration.

## Deliberate omissions

**Roots** let a client name directories or files that a server may work with.
The specification restricts their URIs to `file://`. A root names a location;
it does not transfer files or grant access to the client's filesystem.

That leaves roots useful only when a local server can already access those
paths, typically over stdio. A remote HTTP server cannot read the client's
files from a path alone, and the browser console cannot offer its host
filesystem. The specification provides no remote-access mechanism to bridge
that gap. This package therefore omits roots: pass required paths through tool
arguments or server configuration instead. SEP-2577 also marks the standalone
roots wire API as deprecated in `2026-07-28`; the limited filesystem model is
the reason for the omission across all revisions.

## What costs something

Two, worth knowing before you rely on them.

**Asking on `2025-03-26` costs model context.** The question and its answers
pass through the model as tool arguments, because the revision has no other
channel. Only tools whose handler takes the `Exchange` carry the two extra
arguments, so nothing else is affected.

**A pushed question holds a connection.** On `2025-11-25` and `2025-06-18` the
server keeps the call's stream open while a person decides. `ask_timeout_seconds`
bounds it; the default is two minutes.

## What it takes

`Registry` defaults to an in-memory session store and hub. With
[shared backends](../deployment/stores.md), an interaction can span workers.

## Client compatibility

Client defaults depend on the installed version and configuration. Inspect the
actual discovery or initialization exchange instead of assuming support from a
product name. The bundled console can select each supported revision and show
the wire messages.

For Skills, both the protocol revision and the extension declaration matter.
On `2026-07-28`, check `capabilities.extensions` before calling `skills/list`
or `skills/get`. On older revisions, use the resource fallback. A client that
can read a skill file does not necessarily support automatic skill loading.
