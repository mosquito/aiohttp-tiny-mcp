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

**Schemas the older clients cannot read.** A tool whose input schema uses
composition or `$ref` is simplified where that is lossless, and hidden where it
is not. Declaring `min_revision` says so explicitly; the rest is decided in one
place, by the adapter.

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

**Tool names.** The specification's rule is a character set and a length, and
this package refuses a name that breaks it at registration. The official SDK
logs a warning and registers the tool anyway. A server that a host would reject
therefore does not start here, which is deliberate: every comparable mistake in
a declaration also raises at registration. See
[Names and addresses](../concepts.md#names-and-addresses).

**Resource templates.** Only a simple `{name}` variable is supported, matching
one path segment. An RFC 6570 operator such as `{+path}`, a repeated variable
and two adjacent variables are refused, where the SDK implements more of
RFC 6570.

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

A session store and a hub, both required. See
[Stores and hubs](../deployment/stores.md). Given a shared pair, nothing pins a
client to a worker: a round trip started on one finishes on another, and a
question asked by one is answered through another.

## Client compatibility

Client defaults depend on the installed version and configuration. Inspect the
actual discovery or initialization exchange instead of assuming support from a
product name. The bundled console can select each supported revision and show
the wire messages.

For Skills, both the protocol revision and the extension declaration matter.
On `2026-07-28`, check `capabilities.extensions` before calling `skills/list`
or `skills/get`. On older revisions, use the resource fallback. A client that
can read a skill file does not necessarily support automatic skill loading.
