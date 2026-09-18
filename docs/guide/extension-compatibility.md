# Extension compatibility through resources

An application registers an `Extension` once. On MCP `2026-07-28`, its methods
use native extension discovery and dispatch. On `2024-11-05`, `2025-03-26`,
`2025-06-18`, and `2025-11-25`, the library publishes the same registered
interface as ordinary MCP resources.

`mcp-extensions://` is an application-defined URI scheme. It is not a new MCP
method, capability, or resource type. The resource specifications permit custom
URI schemes and parameterized resources:
[2024-11-05](https://modelcontextprotocol.io/specification/2024-11-05/server/resources),
[2025-03-26](https://modelcontextprotocol.io/specification/2025-03-26/server/resources),
[2025-06-18](https://modelcontextprotocol.io/specification/2025-06-18/server/resources),
and [2025-11-25](https://modelcontextprotocol.io/specification/2025-11-25/server/resources).

## The protocol boundary

| Operation | MCP `2026-07-28` | Older revisions |
| --- | --- | --- |
| Initialize or discover | `server/discover`, with `capabilities.extensions` | `initialize`, with the ordinary `resources` capability; no extension capability is added |
| Discover callable interfaces | Native extension declarations | `resources/list`, `resources/templates/list`, and a JSON manifest resource |
| Invoke a registered method | Its JSON-RPC method name | `resources/read` with a namespaced URI |
| Return its value | Native method result | JSON serialized into `result.contents[0].text` |
| Report failure | JSON-RPC error | JSON-RPC error from `resources/read` |
| Display in the console | **Extensions** and **Skills** views | **Resources** and **Resource templates** only |

On an older revision, direct requests for `server/discover` or a registered
custom method remain unavailable. Registering an extension does not change that
revision's method map. The resource mapping does not advertise or enable native
Tasks, Apps, or other optional capabilities.

## Discover the complete interface

Call `resources/list` and `resources/templates/list`. Follow each listing's
`nextCursor` until all pages have been read. The full extension identifier is
part of every compatibility URI:

```text
mcp-extensions://io.modelcontextprotocol/skills/manifest.json
mcp-extensions://io.modelcontextprotocol/skills/skills/list
mcp-extensions://io.modelcontextprotocol/skills/skills/get?params={params}
mcp-extensions://io.modelcontextprotocol/skills/deploy/SKILL.md
```

Read `manifest.json` through `resources/read`. Its JSON content contains:

- `instructions`: how to invoke the mapped methods.
- `methodResources`: each method's URI template, input schema, description,
  and output schema when the handler declares a Pydantic return model.
- `resources` and `resourceTemplates`: fixed addresses and parameterized routes.
- `name`, `capabilities`, and `methods`: descriptive application data.

The `capabilities` field inside this JSON document is resource content. It is
not the `capabilities` field of an MCP initialization response.

Every registered method gets a parameterized route automatically. If its input
schema has no required parameters, it also gets a fixed URI for an empty argument
object. Listing routes or reading the manifest does not invoke method handlers.

## Invoke a method using a resource read

For a method without arguments, send a normal resource request:

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "resources/read",
  "params": {
    "uri": "mcp-extensions://example.org/jobs/jobs/start"
  }
}
```

A handler can return extension-specific data. For example, a task-shaped value
is carried as text inside the standard resource response:

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "result": {
    "contents": [{
      "uri": "mcp-extensions://example.org/jobs/jobs/start",
      "mimeType": "application/json",
      "text": "{\"resultType\":\"task\",\"task\":{\"taskId\":\"a\"}}"
    }]
  }
}
```

The outer MCP result has only `contents`. It does not acquire `resultType`,
`task`, `ttlMs`, or other fields from the extension value. The caller parses
`text` as application JSON. A value inside that string does not change the
negotiated protocol revision or make the host a native Tasks client.

For arguments, serialize the complete argument object as JSON, percent-encode
it once, and substitute it for `{params}`. For example, `{"taskId":"a"}` becomes:

```text
mcp-extensions://example.org/jobs/tasks/get?params=%7B%22taskId%22%3A%22a%22%7D
```

Fields such as `inputResponses` remain inside that encoded object. They are not
added to the outer `resources/read` parameters. The server decodes the object,
validates it with the method's Pydantic model, resolves dependencies, and invokes
the same handler. Invalid arguments produce `-32602`; a handler's `Rejected`
failure retains its mapped error code, message, and data.

## Files, pagination, and dynamic data

The library translates `uri` fields in method arguments and results between
canonical addresses and namespaced addresses for files registered on that
extension. File templates also receive namespaced equivalents. Text and binary
file contents remain unchanged; returned resource content identifies the
requested URI. Resources registered directly on `Registry` do not acquire an
extension namespace.

Cursors remain opaque. A `nextCursor` inside a method's JSON content belongs to
that method: put it into the next encoded argument object as `cursor`. It is
separate from the outer pagination of `resources/list`.

The manifest captures route declarations when `registry.extension()` installs
them. Each method resource read invokes its handler again, so dynamic catalogs,
job state, updates, and cancellation do not require rebuilding the registry.
See [Dynamic skills](dynamic-skills.md) for live file handlers.

## Behavior and limits

All registered methods are mapped, including methods that change state.
Reading a mapped `tasks/update` or `tasks/cancel` resource invokes that operation.
This is this library's application convention; the older MCP specifications do
not define extension invocation semantics for resource reads. The descriptions
and manifest identify method resources as invocations. Clients must invoke them
deliberately and must not substitute cached content for a requested operation.

The mapping exposes implemented handlers. It does not implement an extension's
business logic, durable task storage, an Apps iframe host, or OAuth token flows.
It does not synthesize native extension notifications or teach older clients
automatic polling or skill activation. An agent can read the manifest and use
the complete registered request/resource interface through standard resource
operations; specialized host behavior still requires host support.

Transport requirements remain revision-specific. In particular, MCP
`2024-11-05` predates Streamable HTTP. The library can accept that revision over
its HTTP endpoint, but this does not redefine the historical transport. See
[Transports](../deployment/transports.md).

## Regression checks

`tests/test_extension_wire.py` captures requests and responses over stdio and
HTTP for all four older revisions. It checks unchanged method maps, resource-only
capability advertisement, ordinary listing/read envelopes, and rejection of
native extension calls. It also checks that extension-specific fields remain
inside resource text and that discovery does not invoke handlers.
