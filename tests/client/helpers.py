from mcp.server.mcpserver import MCPServer
from mcp_types import Completion
from pydantic import BaseModel


class Sum(BaseModel):
    result: int


def build_mcpserver_app():
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

    return server.streamable_http_app(stateless_http=True)
