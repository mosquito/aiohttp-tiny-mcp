# Prompts

A prompt is a starting point a person chooses -- from a menu, a slash command,
a palette. It expands into one or more messages. See
[Tools, resources, and prompts](../concepts.md).

This is where a server offers its own expertise about how to ask: the phrasing
that gets good results, the context worth including, the shape of a useful
conversation. A tool is what the model decides to do; a prompt is what the
person decides to start.

<!-- name: test_prompts -->
```python
from pydantic import BaseModel, Field

from aiohttp_tiny_mcp import MemoryHub, MemorySessionStore, Registry

registry = Registry("review", "1.0", hub=MemoryHub(), session_store=MemorySessionStore())
```

## Declaring one

<!-- name: test_prompts -->
```python
class Review(BaseModel):
    language: str = Field(description="The language the code is written in.")
    strictness: str = "normal"


@registry.prompt
async def code_review(args: Review) -> str:
    """Review a piece of code for correctness and clarity."""
    return (
        f"Review the following {args.language} code. "
        f"Be {args.strictness} about correctness, naming and error handling."
    )
```

The argument model becomes the prompt's declared arguments. Each field's
description reaches the person filling it in, and a field without a default is
marked required.

<!-- name: async test_prompts; fixtures: serve -->
```python
from aiohttp_tiny_mcp import Client
from aiohttp_tiny_mcp.protocol.selection import AdapterSet

url = await serve(registry)
async with Client(url, AdapterSet.default().by_version["2026-07-28"]) as client:
    await client.initialize()
    listed = await client.list_prompts()
    result = await client.get_prompt("code_review", {"language": "python"})

declared = {argument.name: argument.required for argument in listed[0].arguments}
assert declared == {"language": True, "strictness": False}
assert "python code" in result.messages[0].content.text
```

Prompt arguments are strings on the wire, whatever the model says, because a
person types them into a box. Keep them to strings, or accept that pydantic
will coerce.

## Returning several messages

A string becomes one user message. Return a list to shape a conversation --
several turns, or an assistant turn that primes the answer.

<!-- name: test_prompts -->
```python
from aiohttp_tiny_mcp.protocol.models import PromptMessage, TextContent


class Explain(BaseModel):
    topic: str


@registry.prompt
async def explain(args: Explain) -> list[PromptMessage]:
    """Explain something, starting from what the reader already knows."""
    return [
        PromptMessage(
            role="user",
            content=TextContent(text=f"Explain {args.topic}."),
        ),
        PromptMessage(
            role="assistant",
            content=TextContent(text="First, what do you already know about it?"),
        ),
    ]
```

Return a `GetPromptResult` where the prompt needs a description of its own
alongside the messages.

## Names and titles

<!-- name: test_prompts -->
```python
class Nothing(BaseModel):
    pass


@registry.prompt(name="summarise", title="Summarise the conversation")
async def summarize_conversation(args: Nothing) -> str:
    """Condense what has been said so far."""
    return "Summarise the conversation so far in five bullet points."
```

`name` is the identifier a client uses; `title` is what a person reads in the
menu. Where a title is absent the name is shown, so name prompts for people.

## Suggesting argument values

A person filling in `language` should not have to guess what is accepted. See
[Completion](completion.md).

## A prompt may ask, too

Prompt handlers can call `ex.ask()`. Pass `on_ask` to answer questions through
`get_prompt()` on `2026-07-28`, `2025-11-25`, and `2025-06-18`. On
`2026-07-28`, the Python client retries the request with the answers and any
request state. If a callback is missing, it raises `ClientError` with the
`input_required` result in `error.data`.

On `2025-03-26` and `2024-11-05`, call `mcp_resources_read` through
`Client.call_tool()` so the answers can travel as tool arguments.
See [Asking the user](asking.md).

## Clients without native prompts

Add `prompts=resource` to the endpoint URL to list prompts as native resource
templates. A client that uses `resources=tool` finds the same addresses with
`mcp_resources_list` and reads them with `mcp_resources_read`.
Over stdio, pass `prompts="resource"` to `run_stdio()` to add the templates to
the native catalogue, or pass `resources="tool"` to use the resource tools.

An address starts with `mcp-prompt:///` and the URL-encoded prompt name.
Replace `{arguments}` with a percent-encoded JSON object. Omit the query
when the prompt accepts an empty argument object. A read uses the same prompt
handler and argument validation as `prompts/get`. The result contains
`messages` and any `description` returned by the handler.

<!-- name: async test_prompts; fixtures: serve -->
```python
import json
from urllib.parse import quote

url = await serve(registry)
endpoint = url + "?prompts=resource"
async with Client(endpoint, AdapterSet.default().by_version["2026-07-28"]) as client:
    await client.initialize()
    template = next(
        item
        for item in await client.list_resource_templates()
        if item.uri_template.startswith("mcp-prompt:///code_review?")
    )
    arguments = quote(json.dumps({"language": "python"}), safe="")
    uri = template.uri_template.replace("{arguments}", arguments)
    result = await client.read_resource(uri)
    prompt = json.loads(result.contents[0].text)
    assert "python code" in prompt["messages"][0]["content"]["text"]
```
