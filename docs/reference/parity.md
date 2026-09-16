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
| **Logging** | per request | per session | per session | per session | per session |
| **Subscriptions** | `subscriptions/listen` | `resources/subscribe` + `GET` | same | same | same |
| **Sessions** | by handle | by `Mcp-Session-Id` | same | same | same |
| Tool hints (`annotations`) | yes | yes | yes | yes | no |
| Display titles | yes | yes | yes | no | no |
| Cache hints on results | yes | no | no | no | no |
| Batching | no | no | no | yes | no |
| Header mirroring | yes | no | no | no | no |

Rows in bold are the ones where the mechanism differs but the application code
does not. Everything else is either present everywhere or a property of the
revision that no amount of work can add.

## What is genuinely lost

Three things, and each is a property of the older revision rather than a gap
here.

**Structured output.** A tool returning a bare list gets `structuredContent` on
`2026-07-28` and text on `2025-11-25` and `2025-06-18`, which require an object
there. `2025-03-26` and `2024-11-05` predate the field entirely and get the
same data as text, which is where a tool of those revisions is supposed to put
it. The console reads it back either way, so it looks the same to a person.

**Schemas the older clients cannot read.** A tool whose input schema uses
composition or `$ref` is simplified where that is lossless, and hidden where it
is not. Declaring `min_revision` says so explicitly; the rest is decided in one
place, by the adapter.

**Cache hints.** `ttlMs` and `cacheScope` exist only on `2026-07-28`.

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

A snapshot from 2026-08-19, kept because the next person should not have to
re-derive it before proposing to narrow revision support.

| Client | Speaks 2026-07-28 | Default |
| --- | --- | --- |
| Claude, Claude Code | announced day-of-release | rollout in progress; negotiated 2025-11-25 in a live test |
| Codex CLI | shipped August 2026 | opt-in (`CODEX_MCP_PROTOCOL_VERSION`); legacy unless set |
| pi.dev | can be pinned | `"legacy"` -- classic `initialize` |
| OpenCode (`mcp-go`) | library supports it | negotiates down unless the server opts into `Stateless=true` |
| LangChain MCP adapters | Python SDK v2 lands it | migration incomplete |
| Gemini CLI | unconfirmed | struggling even with 2025-11-25 |
| Official SDKs | yes, v2.0.0+ | and still serve every 2025-era client from one server |

The last row is the point: the implementations with the most resources chose to
keep serving the older revisions rather than force a cutover, using the same
shape as here -- one adapter per revision over a normalized core.
