from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from aiohttp.test_utils import TestClient, TestServer
from cryptography.hazmat.primitives.asymmetric import rsa

from aiohttp_tiny_mcp import (
    Endpoint,
)
from aiohttp_tiny_mcp.auth.jwt import HMACJWTVerifier, PublicKeyJWTVerifier
from tests.auth.helpers import PSK, build, pem


@pytest.fixture
async def served() -> AsyncIterator[TestClient]:
    registry, _ = build()
    server = TestServer(Endpoint(registry).app("/mcp"))
    client = TestClient(server)
    await client.start_server()
    try:
        yield client
    finally:
        await client.close()


@pytest.fixture(scope="session")
def key_pair():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="session")
def rsa_verifier(key_pair):
    return PublicKeyJWTVerifier(pem(key_pair))


@pytest.fixture(scope="session")
def hmac_verifier():
    return HMACJWTVerifier(PSK)
