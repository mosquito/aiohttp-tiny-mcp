"""The official SDK's server over stdin and stdout, offering the same tools."""

from __future__ import annotations

from mcp.server.mcpserver import MCPServer
from pydantic import BaseModel

GREETING = "hello"


class Sum(BaseModel):
    result: int


server = MCPServer("bench")


@server.tool()
def add(a: int, b: int) -> Sum:
    return Sum(result=a + b)


@server.tool()
def text() -> str:
    return GREETING


if __name__ == "__main__":
    server.run()
