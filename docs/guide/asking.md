# Asking the user

Use `ex.ask` when an operation needs a user's decision or a missing value.
The client presents the question and returns an answer; your handler decides
what to do with acceptance, refusal, or cancellation.

For a remote server, that answer may arrive in a separate HTTP request on
another worker. The library uses the shared hub for replies to waiting calls,
and the shared store for explicitly saved state when a follow-up call restarts
the handler. See [How the server fits together](../pieces.md).

**Write the handler so it can run again.** Some revisions keep the original call
open; others end it with a question and restart the handler when the client
answers. Keep irreversible work after the final question. Python local variables
are not saved automatically; explicit state is covered below.

<!-- name: async test_asking; fixtures: serve -->
```python
from pydantic import BaseModel

from aiohttp_tiny_mcp import Client, Exchange, Registry, elicit, elicit_accept
from aiohttp_tiny_mcp.protocol.selection import AdapterSet

registry = Registry("deployer", "1.0")


class Deploy(BaseModel):
    service: str


@registry.tool
async def deploy(args: Deploy, ex: Exchange) -> str:
    """Deploy a service, once somebody agrees to it."""
    agreed = await ex.ask("confirm", elicit(f"Deploy {args.service}?"))
    if not agreed.accepted:
        return f"stopped at {agreed.action}"
    return f"deployed {args.service}"
```

The `Exchange` arrives because the handler asked for it by annotation -- it is
the context of this invocation. [Using Exchange](exchange.md) explains its
lifetime and methods. `ask` takes a key, which names
this question within the call, and a request built by `elicit`.

## The same call, every revision

<!-- name: async test_asking -->
```python
async def answer(request):
    return elicit_accept({})


url = await serve(registry)
for adapter in AdapterSet.default().adapters:
    async with Client(url, adapter, on_ask=answer) as client:
        await client.initialize()
        result = await client.call_tool("deploy", {"service": "web"})
    assert result.content[0].text == "deployed web", adapter.version
```

Five revisions, three mechanisms, one handler and one caller.

## What actually happens

`2026-07-28` -- **MRTR.** The question *is* the result: the server answers with
`resultType: "input_required"`, and the client calls again with the answers and
the `requestState` it was given. The handler runs from the beginning each time
and `ask` returns immediately for anything already answered.

`2025-11-25` and `2025-06-18` -- **a pushed request.** The server sends
`elicitation/create` on the stream of the call in flight and waits. The client
answers with a plain JSON-RPC response, sent on its own. The call stays open in
between.

`2025-03-26` and `2024-11-05` -- **the tool call.** Both predate elicitation.
The first call returns an ordinary result whose text says what is needed, and
the answers come back as two extra tool arguments. See
[Asking where the protocol cannot](../reference/revisions.md#asking-where-the-protocol-cannot).

## Asking for data, not just agreement

Pass a JSON Schema and the answer carries content.

<!-- name: async test_asking_schema; fixtures: serve -->
```python
from pydantic import BaseModel

from aiohttp_tiny_mcp import Client, Exchange, Registry, elicit, elicit_accept
from aiohttp_tiny_mcp.protocol.selection import AdapterSet

registry = Registry("deployer", "1.0")


class Nothing(BaseModel):
    pass


@registry.tool
async def rename(args: Nothing, ex: Exchange) -> str:
    """Ask for a new name."""
    answer = await ex.ask(
        "name",
        elicit(
            "What should it be called?",
            {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
        ),
    )
    return f"renamed to {answer['name']}" if answer.accepted else "kept"


async def answer(request):
    return elicit_accept({"name": "widget"})


url = await serve(registry)
adapter = AdapterSet.default().by_version["2026-07-28"]
async with Client(url, adapter, on_ask=answer) as client:
    await client.initialize()
    result = await client.call_tool("rename", {})

assert result.content[0].text == "renamed to widget"
```

An `Answer` reads like the content it carries -- `answer["name"]` -- and also
reports how it was answered.

## Refusal is not failure

There are three ways a question can end, and they mean different things. A
refusal is an answer; an abandonment is not.

<!-- name: async test_asking_actions; fixtures: serve -->
```python
from pydantic import BaseModel

from aiohttp_tiny_mcp import (
    Client,
    Exchange,
    Registry,
    elicit,
    elicit_accept,
    elicit_cancel,
    elicit_decline,
)

registry = Registry("deployer", "1.0")
from aiohttp_tiny_mcp.protocol.selection import AdapterSet


class Nothing(BaseModel):
    pass


@registry.tool
async def gate(args: Nothing, ex: Exchange) -> str:
    """Report exactly how the question was answered."""
    answer = await ex.ask("confirm", elicit("Go ahead?"))
    return f"{answer.action.value}"


url = await serve(registry)
adapter = AdapterSet.default().by_version["2025-11-25"]

for build, expected in (
    (elicit_accept({}), "accept"),
    (elicit_decline(), "decline"),
    (elicit_cancel(), "cancel"),
):

    async def answer(request, reply=build):
        return reply

    async with Client(url, adapter, on_ask=answer) as client:
        await client.initialize()
        result = await client.call_tool("gate", {})
    assert result.content[0].text == expected
```

`accepted` is true only for `accept`. Read `action` where the difference
matters: `decline` is a person saying no, `cancel` is a person who never
answered.

## Where nobody can answer

`ex.ask(default=...)` uses the default if the revision requires clients to
declare elicitation and the client omits it.

<!-- name: async test_asking_default; fixtures: serve -->
```python
from pydantic import BaseModel

from aiohttp_tiny_mcp import Client, Exchange, Registry, elicit, elicit_decline
from aiohttp_tiny_mcp.protocol.selection import AdapterSet

registry = Registry("deployer", "1.0")


class Nothing(BaseModel):
    pass


@registry.tool
async def careful(args: Nothing, ex: Exchange) -> str:
    """Refuse by default where the question cannot be put."""
    answer = await ex.ask("confirm", elicit("Go ahead?"), default=elicit_decline())
    return "went ahead" if answer.accepted else f"defaulted to {answer.action.value}"


url = await serve(registry)
# This client has no handler for pushed questions.
async with Client(url, AdapterSet.default().by_version["2025-11-25"]) as client:
    await client.initialize()
    result = await client.call_tool("careful", {})

assert result.content[0].text == "defaulted to decline"
```

Without a default, a missing elicitation capability produces an error with
`data.requiredCapabilities`. The code is `-32021` on `2026-07-28` and
`-32603` on the two revisions that push questions.

## Several questions

Ask as many as the work needs. On a revision that pushes, each is a round trip
on the open stream. On `2026-07-28` the handler restarts for each, so keep
anything expensive after the last question, or behind a check.

<!-- name: async test_asking_several; fixtures: serve -->
```python
from pydantic import BaseModel

from aiohttp_tiny_mcp import Client, Exchange, Registry, elicit, elicit_accept
from aiohttp_tiny_mcp.protocol.selection import AdapterSet

registry = Registry("deployer", "1.0")

VALUE = {"type": "object", "properties": {"value": {"type": "string"}}}


class Nothing(BaseModel):
    pass


@registry.tool
async def interview(args: Nothing, ex: Exchange) -> str:
    """Two questions, in order."""
    first = await ex.ask("first", elicit("Your name?", VALUE))
    second = await ex.ask("second", elicit("Your quest?", VALUE))
    return f"{first['value']} seeks {second['value']}"


asked = []


async def answer(request):
    asked.append(request["params"]["message"])
    return elicit_accept({"value": f"answer-{len(asked)}"})


url = await serve(registry)
async with Client(url, AdapterSet.default().by_version["2026-07-28"], on_ask=answer) as client:
    await client.initialize()
    result = await client.call_tool("interview", {})

assert asked == ["Your name?", "Your quest?"]
assert result.content[0].text == "answer-1 seeks answer-2"
```

The client sends every answer it has collected back on each attempt, not only
the newest, because the handler starts from the beginning and would otherwise
ask the same thing forever.

## Carrying state between the calls

A handler that computed something expensive before asking can keep it, rather
than computing it again on the next attempt.

<!-- name: async test_asking_state; fixtures: serve -->
```python
from pydantic import BaseModel

from aiohttp_tiny_mcp import Client, Exchange, NeedInput, Registry, elicit, elicit_accept
from aiohttp_tiny_mcp.protocol.selection import AdapterSet

registry = Registry("deployer", "1.0")

computed = []


class Target(BaseModel):
    name: str


class Nothing(BaseModel):
    pass


@registry.tool
async def purge(args: Target, ex: Exchange) -> str:
    """Look the target up once, then confirm."""
    if ex.answered("confirm"):
        return f"purged {ex.state['resolved']}" if ex.accepted("confirm") else "kept"
    computed.append(args.name)
    raise NeedInput(
        {"confirm": elicit(f"Really purge {args.name}?")},
        state={"resolved": args.name.upper()},
    )


async def answer(request):
    return elicit_accept({})


url = await serve(registry)
async with Client(url, AdapterSet.default().by_version["2026-07-28"], on_ask=answer) as client:
    await client.initialize()
    result = await client.call_tool("purge", {"name": "cache"})

assert result.content[0].text == "purged CACHE"
assert computed == ["cache"]
```

`NeedInput` is the explicit form of what `ask` raises for you. The state stays
on the server; the client carries only an identifier for it. See
[Sessions and state](sessions.md#state-between-two-calls) for what that
guarantees.

## The client's side

Pass `on_ask` to answer questions automatically. On `2025-11-25` and
`2025-06-18`, the callback also enables the elicitation capability.

On `2026-07-28`, the client declares elicitation automatically. On the two
oldest revisions, tools can ask through their arguments. On these three
revisions, `call_tool()` returns a plain dict if it has no callback to answer
the question. Pass the answers into the next call.

<!-- name: async test_asking_manual; fixtures: serve -->
```python
from pydantic import BaseModel

from aiohttp_tiny_mcp import Client, Exchange, Registry, elicit, elicit_accept
from aiohttp_tiny_mcp.protocol.selection import AdapterSet

registry = Registry("deployer", "1.0")


class Nothing(BaseModel):
    pass


@registry.tool
async def gate(args: Nothing, ex: Exchange) -> str:
    """One confirmation."""
    return "went ahead" if (await ex.ask("confirm", elicit("Go ahead?"))).accepted else "stopped"


url = await serve(registry)
adapter = AdapterSet.default().by_version["2026-07-28"]

async with Client(url, adapter) as client:
    await client.initialize()
    asked = await client.call_tool("gate", {})
    requests, state = adapter.client_input_requests(asked)
    assert list(requests) == ["confirm"]

    done = await client.call_tool(
        "gate", {}, input_responses={"confirm": elicit_accept({})}, request_state=state
    )

assert done.content[0].text == "went ahead"
```

## Completions: asking the client's model

A question goes to a person. `ex.completion` goes to the client's model, which is
the other thing a server may ask for:

<!-- name: async test_asking_completion; fixtures: serve -->
```python
from pydantic import BaseModel

from aiohttp_tiny_mcp import Client, Exchange, Registry, CompletionMessage, TextContent
from aiohttp_tiny_mcp.protocol.selection import AdapterSet

registry = Registry("writer", "1.0")


class Topic(BaseModel):
    about: str


@registry.tool
async def headline(args: Topic, ex: Exchange) -> str:
    """Have the client's model write a headline."""
    answered = await ex.completion(
        messages=[
            CompletionMessage(
                role="user", content=TextContent(text=f"A headline about {args.about}")
            )
        ],
        max_tokens=32,
        system_prompt="Answer with one line.",
    )
    return answered.text


async def complete(request):
    """A client answers with a completion, in the shape the specification states."""
    return {
        "role": "assistant",
        "content": {"type": "text", "text": "Trees, mostly"},
        "model": "example-model",
    }


url = await serve(registry)
async with Client(url, AdapterSet.default().adapters[0], on_completion=complete) as client:
    await client.initialize()
    written = await client.call_tool("headline", {"about": "trees"})

assert written.content[0].text == "Trees, mostly"
```

`max_tokens` is required by the specification. `system_prompt`,
`model_preferences`, `include_context`, `temperature`, `stop_sequences` and
`metadata` are optional and passed through. The client chooses the model, so
preferences are a request and not an instruction; the answer says which model
replied.

The result is a `CompletionResult`: `role`, `content`, `model`, an optional
`stop_reason`, and `.text` for the common case of a text completion. A client
that refuses, or answers something that is not a completion, fails the call —
a completion that did not happen is not an empty completion.

Model completions reach every revision: pushed on the call's stream where there is
one, and carried in the result on `2026-07-28`. The adapter chooses the channel;
the handler calls `ex.completion()` on every revision. Its keyword arguments
build a `CompletionRequest`. On `2026-07-28`, the handler
restarts with the answer, so code before the call may run again.

`ex.completion()` reuses an answer the call already carries. `ex.completed(key)` lets
the handler inspect that completion without making a request:

<!-- name: test_asking_completion_guard -->
```python
async def careful(args, ex):
    """Request one completion, even where the handler restarts."""
    answered = ex.completed() or await ex.completion(
        messages=[{"role": "user", "content": {"type": "text", "text": "once"}}], max_tokens=16
    )
    return answered.text
```

A handler that requests more than one completion passes `key=` to keep the answers apart.
`Registry(completion_timeout_seconds=...)` bounds each pushed completion wait,
separately from `ask_timeout_seconds`. The default is five minutes. On
`2026-07-28`, MRTR returns the request immediately; the client controls how
long it takes to submit the answer. The pushed-wait timeout does not apply.
`request_state_ttl_seconds` limits stored handler state only when the handler
supplies state. It is not a deadline for a stateless completion request.

A client declares `sampling` only when it has an `on_completion` callback, so a
server refuses the call with `requiredCapabilities: {"sampling": {}}` rather
than waiting for an answer nobody can give. In the bundled console, select
**Can answer for a model** before connecting to enter the completion yourself.
The dialog shows the completion request and replies as `console-operator`.
See [The console](console.md#questions) and the `summarize` tool in
`examples/demo.py` for a runnable example.

`sampling.tools`, where a model may call tools during the completion, is not
implemented here.
