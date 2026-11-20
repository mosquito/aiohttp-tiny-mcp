import pytest
from pydantic import BaseModel

from aiohttp_tiny_mcp import MemoryHub, Registry
from aiohttp_tiny_mcp.auth import Authorization, Principal, StaticVerifier


class Nothing(BaseModel):
    pass


class Entry(BaseModel):
    id: int


@pytest.fixture
def scoped_registry() -> Registry:
    tokens = {
        name: Principal(subject="same-user", namespace="tenant", scopes=frozenset(scopes))
        for name, scopes in (("weak", []), ("strong", ["files:read"]))
    }
    scoped_registry = Registry(
        "resources",
        "1",
        hub=MemoryHub(),
        hub_poll_seconds=0.01,
        auth=Authorization(
            verifier=StaticVerifier(tokens),
            resource="https://example.test/mcp",
            scopes_supported=["files:read"],
        ),
    )

    @scoped_registry.resource("files://public")
    async def public(args: Nothing) -> str:
        return "public"

    @scoped_registry.resource("files://private", scopes={"files:read"})
    async def private(args: Nothing) -> str:
        return "private"

    @scoped_registry.resource("files://entries/{id}", scopes={"files:read"})
    async def entry(args: Entry) -> str:
        return str(args.id)

    return scoped_registry
