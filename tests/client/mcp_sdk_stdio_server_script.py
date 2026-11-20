"""Official SDK stdio server fixture, spawned by test_stdio_client_against_mcp_sdk.py."""

from __future__ import annotations

from mcp.server.mcpserver import MCPServer
from mcp_types import Completion
from pydantic import BaseModel


class Sum(BaseModel):
    result: int


server = MCPServer("demo")


@server.tool()
def add(a: int, b: int) -> Sum:
    return Sum(result=a + b)


@server.tool()
def boom() -> str:
    raise RuntimeError("kaboom")


@server.resource("config://app")
def config() -> dict:
    return {"debug": False}


@server.resource("res://items/{id}")
def item(id: str) -> str:
    return f"item-{id}"


@server.prompt()
def greet(language: str) -> str:
    return f"Hello, {language} speaker."


@server.completion()
async def complete(ref, argument, context):
    if argument.name == "language":
        values = [x for x in ("python", "rust", "go") if x.startswith(argument.value)]
        return Completion(values=values)
    return Completion(values=[])


if __name__ == "__main__":
    server.run()
