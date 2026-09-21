"""Built-in encrypted bearer tokens preserve credentials and reject invalid envelopes."""

import hashlib
import hmac
import os
import time
import zlib
from base64 import urlsafe_b64decode, urlsafe_b64encode
from dataclasses import asdict, replace

import pytest

from aiohttp_tiny_mcp import Principal
from aiohttp_tiny_mcp.oauth import AbstractCipher, EncryptedTokens, KECCAKCipher

ISSUER = "https://mcp.example/oauth"
RESOURCE = "https://mcp.example/mcp"


@pytest.mark.parametrize(
    "data",
    [
        {},
        {"x": ""},
        {"x": "x"},
        {"x": list(range(256))},
        {"x": os.urandom(1024).hex()},
        {"x": "x" * 10000},
    ],
)
def test_compression_roundtrip_handles_expansion_and_binary_data(data):
    cipher = KECCAKCipher(os.urandom(32))
    assert cipher.decode(cipher.encode(data)) == data


def test_repeated_data_is_compressed():
    assert len(KECCAKCipher(os.urandom(32)).encode({"x": "repeated" * 1000})) < 200


@pytest.mark.parametrize("compression_level", [0, 3, 6, 9])
def test_integer_xor_preserves_zero_bytes(monkeypatch, compression_level):
    cipher = KECCAKCipher(os.urandom(32), compression_level=compression_level)
    payload = {"token": "test"}
    compressed = zlib.compress(cipher.dump_json(payload), level=compression_level)

    class Stream:
        def digest(self, length):
            assert length == len(compressed)
            return compressed

    monkeypatch.setattr("aiohttp_tiny_mcp.oauth.tokens.hashlib.shake_256", lambda data: Stream())
    blob = cipher.encode(payload)
    assert blob[16:-32] == bytes(len(compressed))
    assert cipher.decode(blob) == payload


async def test_authenticated_invalid_compression_returns_none(tokens, key):
    nonce = os.urandom(16)
    invalid = b"not a zlib stream"
    stream = hashlib.shake_256(key + nonce).digest(len(invalid))
    ciphertext = bytes(a ^ b for a, b in zip(invalid, stream, strict=True))
    body = nonce + ciphertext
    token = urlsafe_b64encode(body + hmac.digest(key, body, "sha256")).decode()
    assert await tokens.verify(token) is None


@pytest.fixture
def key():
    return os.urandom(32)


@pytest.fixture
def tokens(key):
    return EncryptedTokens(KECCAKCipher(key), issuer=ISSUER, resource=RESOURCE)


@pytest.fixture
def principal():
    return Principal(
        subject="alice",
        issuer=ISSUER,
        client_id="console",
        scopes=frozenset({"read", "write"}),
        expires_at=time.time() + 300,
        namespace="team",
        claims={"display_name": "Alice"},
    )


async def test_randomized_roundtrip_preserves_credentials(tokens, principal, key):
    upstream = {
        "access_token": "private-provider-access",
        "refresh_token": "private-provider-refresh",
        "custom": {"list": [1, "два", None]},
    }
    first = await tokens.issue(principal, upstream)
    second = await tokens.issue(principal, upstream)
    assert first != second
    assert urlsafe_b64decode(first)[:16] != urlsafe_b64decode(second)[:16]
    assert b"private-provider" not in urlsafe_b64decode(first)
    assert tokens.decode(first)["upstream"] == upstream
    other = EncryptedTokens(KECCAKCipher(key), issuer=ISSUER, resource=RESOURCE)
    recovered = await other.verify(first)
    assert recovered == principal
    assert "private-provider" not in repr(recovered)
    assert key.hex() not in repr(tokens)
    assert repr(key) not in repr(tokens)
    tokens.decode(first)["upstream"]["custom"]["list"].append("local change")
    assert tokens.decode(first)["upstream"] == upstream


@pytest.mark.parametrize("position", [0, 15, 16, -33, -32, -1])
async def test_tampering_rejected_before_decryption(tokens, principal, position, monkeypatch):
    token = await tokens.issue(principal, {})
    blob = bytearray(urlsafe_b64decode(token))
    blob[position] ^= 1
    changed = urlsafe_b64encode(blob).decode()

    def no_decryption(*args):
        pytest.fail("a token with an invalid MAC must not be decrypted")

    monkeypatch.setattr("aiohttp_tiny_mcp.oauth.tokens.hashlib.shake_256", no_decryption)
    assert await tokens.verify(changed) is None
    with pytest.raises(ValueError, match="^invalid token$"):
        tokens.decode(changed)


@pytest.mark.parametrize("token", ["", "!", "%%%", "π", "a" * 63, None, 42])
async def test_malformed_tokens_return_none(tokens, token):
    assert await tokens.verify(token) is None


@pytest.mark.parametrize("length", [0, 1, 16, 31, 32, 47, 48])
async def test_truncated_envelope_returns_none(tokens, length):
    assert await tokens.verify(urlsafe_b64encode(b"x" * length).decode()) is None


async def test_wrong_key_issuer_and_resource_fail(tokens, key, principal):
    token = await tokens.issue(principal, {})
    for verifier in (
        EncryptedTokens(KECCAKCipher(os.urandom(32)), issuer=ISSUER, resource=RESOURCE),
        EncryptedTokens(KECCAKCipher(key), issuer=ISSUER + "/other", resource=RESOURCE),
        EncryptedTokens(KECCAKCipher(key), issuer=ISSUER, resource=RESOURCE + "/other"),
    ):
        assert await verifier.verify(token) is None
        with pytest.raises(ValueError, match="^invalid token$"):
            verifier.decode(token)


async def test_expiry_rejects_verification_and_credential_access(tokens, principal, monkeypatch):
    token = await tokens.issue(principal, {"access_token": "secret"})
    monkeypatch.setattr("aiohttp_tiny_mcp.oauth.tokens.time.time", lambda: principal.expires_at)
    assert await tokens.verify(token) is None
    with pytest.raises(ValueError, match="^invalid token$"):
        tokens.decode(token)


@pytest.mark.parametrize("expiry", [None, True, 0, -1, float("inf"), float("nan")])
async def test_issue_requires_finite_future_expiry(tokens, principal, expiry):
    with pytest.raises(ValueError):
        await tokens.issue(replace(principal, expires_at=expiry), {})


@pytest.mark.parametrize("key", [b"", b"short", b"x" * 31, "x" * 32, None])
def test_key_must_be_bytes_and_at_least_32_bytes(key):
    with pytest.raises(ValueError, match="32 random bytes"):
        KECCAKCipher(key)


@pytest.mark.parametrize("field,value", [("issuer", ""), ("resource", ""), ("issuer", None)])
def test_context_must_be_explicit(key, field, value):
    with pytest.raises(ValueError):
        EncryptedTokens(KECCAKCipher(key), **{"issuer": ISSUER, "resource": RESOURCE, field: value})


@pytest.mark.parametrize("payload", [None, [], {}, {"principal": None}])
async def test_authenticated_malformed_payload_fails_safely(tokens, key, payload):
    token = urlsafe_b64encode(KECCAKCipher(key).encode(payload)).decode()
    assert await tokens.verify(token) is None


async def test_invalid_authenticated_principal_is_rejected(tokens, key, principal):
    value = asdict(principal)
    value["scopes"] = ["read"]
    for changes in (
        {"namespace": []},
        {"subject": {}},
        {"client_id": None},
        {"scopes": "read"},
        {"scopes": [42]},
        {"claims": []},
        {"expires_at": "tomorrow"},
    ):
        data = {"resource": RESOURCE, "principal": {**value, **changes}, "upstream": {}}
        assert (
            await tokens.verify(urlsafe_b64encode(KECCAKCipher(key).encode(data)).decode()) is None
        )


async def test_issue_rejects_other_issuer_and_non_json_values(tokens, principal):
    with pytest.raises(ValueError):
        await tokens.issue(replace(principal, issuer="other"), {})
    with pytest.raises(TypeError):
        await tokens.issue(principal, {"not_json": object()})


async def test_decode_rejects_noncanonical_base64(tokens, principal):
    token = await tokens.issue(principal, {})
    assert await tokens.verify(token + "\n") is None
    assert await tokens.verify(token + "=") is None


async def test_encrypted_tokens_delegates_dict_to_cipher(principal):
    class CustomCipher(AbstractCipher):
        def encode(self, data):
            self.data = data
            assert isinstance(data, dict)
            return b"opaque-cipher-output"

        def decode(self, blob):
            assert blob == b"opaque-cipher-output"
            return self.data

    cipher = CustomCipher()
    tokens = EncryptedTokens(cipher, issuer=ISSUER, resource=RESOURCE)
    encoded = await tokens.issue(principal, {"custom": [1, 2]})
    assert tokens.cipher is cipher
    assert urlsafe_b64decode(encoded) == b"opaque-cipher-output"
    assert tokens.decode(encoded)["upstream"] == {"custom": [1, 2]}
    assert await tokens.verify(encoded) == principal
