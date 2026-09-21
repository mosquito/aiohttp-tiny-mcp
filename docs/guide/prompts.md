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

A prompt handler receives the `Exchange` like any other, so it can ask the user
something while it builds the messages. Whether that is a good idea depends on
your host -- a prompt is already a person interacting -- but nothing stops it.
See [Asking the user](asking.md).
