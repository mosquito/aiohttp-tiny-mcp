import pytest

from aiohttp_tiny_mcp import Endpoint, Registry
from aiohttp_tiny_mcp.auth import Authorization, Principal


class NoTokens:
    """Verifies nothing. These tests are about routing, not about tokens."""

    async def verify(self, token: str) -> Principal | None:
        return None


@pytest.fixture
def auth() -> Authorization:
    return Authorization(
        verifier=NoTokens(),
        resource="https://mcp.example.com/reports/mcp",
        authorization_servers=["https://login.example.com"],
    )


@pytest.fixture
def protected(auth: Authorization) -> Endpoint:
    return Endpoint(Registry("reports", "1.0", auth=auth))
