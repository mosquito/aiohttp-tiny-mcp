"""JWT verification adapted from Backlog, with configurable principal mapping."""

import hmac
import json
import subprocess
import sys
import time
from base64 import urlsafe_b64encode
from dataclasses import replace
from unittest.mock import Mock

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed448, ed25519, rsa
from test_basic_auth import registry_for

from aiohttp_tiny_mcp import Authorization, Principal, TokenVerifier, principal_from_claims
from aiohttp_tiny_mcp.jwt import HMACJWTVerifier, PublicKeyJWTVerifier
from aiohttp_tiny_mcp.testing import over_http

PSK = "test-pre-shared-key-with-at-least-32-bytes"


def token(**claims):
    return jwt.encode({"exp": time.time() + 300, **claims}, PSK, algorithm="HS256")


def pem(key):
    return (
        key.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )


@pytest.fixture(scope="session")
def key_pair():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="session")
def rsa_verifier(key_pair):
    return PublicKeyJWTVerifier(pem(key_pair))


@pytest.fixture(scope="session")
def hmac_verifier():
    return HMACJWTVerifier(PSK)


@pytest.mark.parametrize("algorithm", ["RS256", "RS384", "RS512", "PS256", "PS384", "PS512"])
async def test_rsa_algorithms_follow_key_and_reject_confusion(key_pair, rsa_verifier, algorithm):
    signed = jwt.encode({"exp": time.time() + 300}, key_pair, algorithm=algorithm)
    assert await rsa_verifier.verify(signed)
    assert await rsa_verifier.verify(token()) is None
    assert (
        await rsa_verifier.verify(jwt.encode({"exp": time.time() + 300}, "", algorithm="none"))
        is None
    )
    header, payload, signature = signed.split(".")
    changed = ("A" if signature[0] != "A" else "B") + signature[1:]
    assert await rsa_verifier.verify(f"{header}.{payload}.{changed}") is None
    der = key_pair.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    header, payload, _ = token().split(".")
    message = f"{header}.{payload}".encode()
    signature = urlsafe_b64encode(hmac.digest(der, message, "sha256")).rstrip(b"=")
    assert await rsa_verifier.verify(f"{message.decode()}.{signature.decode()}") is None


@pytest.mark.parametrize(
    ("curve", "algorithm"),
    [(ec.SECP256R1(), "ES256"), (ec.SECP384R1(), "ES384"), (ec.SECP521R1(), "ES512")],
)
async def test_ec_curve_selects_algorithm(curve, algorithm):
    key = ec.generate_private_key(curve)
    verifier = PublicKeyJWTVerifier(pem(key))
    assert verifier.algorithms == (algorithm,)
    assert await verifier.verify(jwt.encode({"exp": time.time() + 300}, key, algorithm=algorithm))
    assert await verifier.verify(token()) is None


@pytest.mark.parametrize("kind", [ed25519.Ed25519PrivateKey, ed448.Ed448PrivateKey])
async def test_eddsa_keys(kind):
    key = kind.generate()
    assert await PublicKeyJWTVerifier(pem(key)).verify(
        jwt.encode({"exp": time.time() + 300}, key, algorithm="EdDSA")
    )


@pytest.mark.parametrize(
    "claims",
    [
        {"exp": 0},
        {"exp": None},
        {"exp": "invalid"},
        {"exp": float("inf")},
        {"exp": "NaN"},
        {"nbf": time.time() + 3600},
        {"iat": time.time() + 3600},
        {"iss": "wrong"},
        {"aud": "wrong"},
        {"sub": 1},
        {"iss": []},
        {"client_id": []},
        {"azp": 1},
        {"scope": {}},
        {"scp": [1]},
    ],
)
async def test_invalid_claims_fail_closed(claims):
    verifier = HMACJWTVerifier(PSK, issuer="issuer", audience="mcp")
    payload = {"exp": time.time() + 300, "iss": "issuer", "aud": "mcp", **claims}
    encoded = jwt.api_jws.encode(json.dumps(payload).encode(), PSK, algorithm="HS256")
    assert await verifier.verify(encoded) is None


async def test_required_expiry_issuer_audience_and_hmac_algorithm(hmac_verifier, key_pair):
    verifier = HMACJWTVerifier(PSK, issuer="issuer", audience="mcp")
    assert await verifier.verify(token(iss="issuer", aud=["mcp", "other"]))
    assert await verifier.verify(token()) is None
    assert await verifier.verify(jwt.encode({"iss": "issuer", "aud": "mcp"}, PSK)) is None
    assert await hmac_verifier.verify(jwt.encode({"exp": time.time() + 300}, "x" * 32)) is None
    assert (
        await hmac_verifier.verify(
            jwt.encode({"exp": time.time() + 300}, PSK * 2, algorithm="HS512")
        )
        is None
    )
    assert (
        await hmac_verifier.verify(
            jwt.encode({"exp": time.time() + 300}, key_pair, algorithm="RS256")
        )
        is None
    )
    assert (
        await hmac_verifier.verify(jwt.encode({"exp": time.time() + 300}, "", algorithm="none"))
        is None
    )
    assert await hmac_verifier.verify("malformed") is None
    assert await hmac_verifier.verify(token(aud="unspecified"))
    assert PSK not in repr(verifier)
    assert isinstance(verifier, TokenVerifier)


async def test_invalid_configuration_and_pem_file(key_pair, tmp_path):
    path = tmp_path / "public.pem"
    path.write_text(pem(key_pair))
    verifier = PublicKeyJWTVerifier(str(path))
    assert await verifier.verify(
        jwt.encode({"exp": time.time() + 300}, key_pair, algorithm="RS256")
    )
    for secret in ["short", pem(key_pair)]:
        with pytest.raises(ValueError):
            HMACJWTVerifier(secret)
    private = key_pair.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    for material in [
        "-----BEGIN PUBLIC KEY-----\ninvalid",
        private,
        pem(rsa.generate_private_key(public_exponent=65537, key_size=1024)),
        pem(ec.generate_private_key(ec.SECP256K1())),
    ]:
        with pytest.raises(ValueError):
            PublicKeyJWTVerifier(material)
    with pytest.raises(OSError):
        PublicKeyJWTVerifier(str(tmp_path / "missing.pem"))


@pytest.mark.parametrize("kind", ["hmac", "rsa"])
async def test_mapper_sees_only_verified_claims_and_can_reject(kind, key_pair):
    seen = Mock(side_effect=lambda claims: replace(principal_from_claims(claims), namespace="team"))
    if kind == "hmac":
        verifier = HMACJWTVerifier(PSK, issuer="issuer", audience="mcp", principal=seen)
        key, algorithm = PSK, "HS256"
    else:
        verifier = PublicKeyJWTVerifier(
            pem(key_pair), issuer="issuer", audience="mcp", principal=seen
        )
        key, algorithm = key_pair, "RS256"
    claims = {
        "sub": "alice",
        "iss": "issuer",
        "aud": "mcp",
        "scope": "read",
        "exp": time.time() + 300,
    }
    encoded = jwt.encode(claims, key, algorithm=algorithm)
    result = await verifier.verify(encoded)
    assert result.namespace == "team"
    seen.assert_called_once_with(claims)
    assert encoded not in repr(result)
    seen.reset_mock()
    for value in [
        "invalid",
        jwt.encode({**claims, "aud": "wrong"}, key, algorithm=algorithm),
        jwt.encode({**claims, "exp": 0}, key, algorithm=algorithm),
    ]:
        assert await verifier.verify(value) is None
    seen.assert_not_called()
    seen.side_effect = ValueError("account disabled")
    assert await verifier.verify(encoded) is None


async def test_mapper_controls_namespace_and_scopes_through_authorization():
    def mapped(claims):
        return replace(
            principal_from_claims(claims), namespace=claims["org"], scopes=frozenset({"read"})
        )

    verifier = HMACJWTVerifier(PSK, principal=mapped)
    registry = registry_for(Authorization(verifier, "https://test/mcp"))
    async with over_http(
        registry, headers={"Authorization": f"Bearer {token(sub='alice', org='team')}"}
    ) as client:
        result = await client.call_tool("who", {})
        assert result.content[0].text == "alice:team"
    assert principal_from_claims({}) == Principal()


def test_core_imports_work_without_jwt_dependencies():
    code = """
import builtins
import sys
original = builtins.__import__
def restricted(name, *args, **kwargs):
    if name.split(".")[0] in {"jwt", "cryptography"}:
        raise ImportError("dependency disabled")
    return original(name, *args, **kwargs)
builtins.__import__ = restricted
from aiohttp_tiny_mcp import Principal, StaticVerifier, principal_from_claims
assert principal_from_claims({}) == Principal()
StaticVerifier({"test": Principal()})
assert "jwt" not in sys.modules
try:
    from aiohttp_tiny_mcp.jwt import HMACJWTVerifier
except ImportError as error:
    assert "aiohttp-tiny-mcp[jwt]" in str(error)
else:
    raise AssertionError("JWT import must explain the missing extra")
"""
    subprocess.run([sys.executable, "-c", code], check=True, capture_output=True, text=True)
