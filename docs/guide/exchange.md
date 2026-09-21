# Using Exchange

`Exchange` is the object a handler uses to communicate with its caller while a
call is running. Returning a value ends the handler's work; the exchange lets
it send progress or a question before that final result, receive the answer,
and access the caller's session.

The library creates one exchange per invocation, with the call ID, client
capabilities, protocol adapter, request, and response channel. These tie
`ex.progress(...)` and `ex.ask(...)` to the correct call even when many clients
are using the same handler concurrently. See
[the confirmation example](../pieces.md#the-ordinary-call)
for the full path from client arguments to a question and final result.

Add an annotated `Exchange` parameter to use these facilities. The library
supplies the object automatically; you do not register an exchange provider or
add it to the argument model. Handlers that just compute and return a value can
omit it.

## Arguments belong to the client; the exchange belongs to the server

<!-- name: async test_exchange; fixtures: serve -->
```python
from pydantic import BaseModel, Field

from aiohttp_tiny_mcp import Client, Exchange, Registry
from aiohttp_tiny_mcp.protocol.selection import AdapterSet

registry = Registry("batches", "1.0")


class Work(BaseModel):
    steps: int = Field(ge=1, le=100)


@registry.tool(streaming=True)
async def process(args: Work, ex: Exchange) -> str:
    """Process a batch and report how many steps are complete."""
    for step in range(args.steps):
        # Do one unit of application work here.
        await ex.progress(step + 1, args.steps)
    return f"processed {args.steps} steps"
```

The input schema contains only `steps`. The name `ex` is a Python convention;
the `Exchange` annotation is what requests injection. Other annotated
parameters can receive [application dependencies](dependencies.md) in the same
way, after you register their providers.

`streaming=True` lets this HTTP tool send progress before its final result.
The client can collect these messages with `on_notification`:

<!-- name: async test_exchange -->
```python
progress = []


async def notification(message):
    if message.get("method") == "notifications/progress":
        progress.append(message["params"]["progress"])


url = await serve(registry)
adapter = AdapterSet.default().by_version["2025-11-25"]
async with Client(url, adapter, on_notification=notification) as client:
    await client.initialize()
    tools = await client.list_tools()
    result = await client.call_tool("process", {"steps": 3})

assert set(tools[0].input_schema["properties"]) == {"steps"}
assert progress == [1, 2, 3]
assert result.content[0].text == "processed 3 steps"
```

## What to use it for

| Need | API | Details |
| --- | --- | --- |
| Report completed work | `await ex.progress(done, total, message=...)` | [Progress](notifications.md#progress) |
| Send a diagnostic message to this client | `await ex.log("info", data)` | Requires a response stream and a client log level; see [Logging](notifications.md#logging) |
| Ask for a decision or missing value | `await ex.ask("question_key", elicit(...))` | Returns an `Answer`; see [Asking the user](asking.md) |
| Inspect client metadata | `ex.client_info` | Client-reported information and capabilities; use [Principal injection](auth.md#handler-permissions) for verified identity |
| Use a transport-provided session | `ex.session` | May be `None`; see [Sessions](sessions.md) |
| Open or use an explicit session handle | `ex.sessions.open()` / `ex.sessions.use(handle)` | Async methods; the caller must send the handle on later calls |
| Read saved state after a question | `ex.state` | State explicitly supplied through `NeedInput`, not a copy of local variables |
| Access the transport request | `ex.request` | An aiohttp request over HTTP; stdio supplies a request-like object |
| Observe cancellation | `ex.cancelled`, `await ex.wait_cancelled()` | Useful for long-running work |

You do not need an exchange to return a result or raise an ordinary handler
exception. Those are handled by the dispatcher.

## Lifetime: an invocation can end before the conversation does

An exchange is local to the worker executing the handler. It can contain an
open stream, resolved dependencies, and cancellation state. It cannot be stored
in a database or transferred to another worker.

This matters when asking a question. For a protocol that delivers a question
on the open stream, `await ex.ask(...)` waits and returns in the same invocation.
For a protocol that delivers it as an input-required result, the handler exits.
The client sends a follow-up call with the answer, potentially to another
worker, and the handler runs again from its first line with a new exchange.

Put irreversible work after the final question, because code before it may run
again. Use stable question keys so the repeated call can match previous answers.
If you need to preserve a computed value, explicitly supply JSON-serializable
state with `NeedInput(..., state=...)`; it will be available as `ex.state` on the
next invocation. The [asking guide](asking.md#carrying-state-between-the-calls)
shows this pattern.

For values shared across independent calls, use application storage or
[sessions](sessions.md). For background resource-change notifications, publish
through the [hub](notifications.md#changes-to-what-the-server-offers). Keeping
an old exchange would tie that work to a finished request and one worker.

Next: [Dependencies](dependencies.md) explains how to supply database clients
and other application objects. [Asking the user](asking.md) builds a handler
that pauses for a decision.
