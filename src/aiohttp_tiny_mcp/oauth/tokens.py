"""Opaque and encrypted OAuth token implementations."""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import re
import secrets
import time
import zlib
from abc import ABC, abstractmethod
from base64 import b64decode, urlsafe_b64encode
from dataclasses import asdict
from typing import Any

from aiohttp_tiny_mcp.auth import Principal
from aiohttp_tiny_mcp.storage.sessions import MemorySessionStore, SessionStore


class OpaqueTokens:
    """Store principals by token digest; discard provider credentials after issue.

    Share the store, issuer and resource across instances. Each token has its
    principal's expiry and can be revoked independently.
    """

    def __init__(self, *, issuer: str, resource: str, store: SessionStore | None = None) -> None:
        if not issuer or not resource:
            raise ValueError("issuer and resource must be non-empty strings")
        self.issuer = issuer
        self.resource = resource
        self.store = store if store is not None else MemorySessionStore()
        context = json.dumps([issuer, resource]).encode()
        self._prefix = "oauth-tokens/" + hashlib.sha256(context).hexdigest() + "/"

    def _key(self, token: str) -> str:
        return self._prefix + hashlib.sha256(token.encode()).hexdigest()

    async def issue(self, principal: Principal, upstream: dict) -> str:
        """Issue an opaque token. Provider credentials are not retained."""
        if (
            principal.issuer != self.issuer
            or principal.expires_at is None
            or not math.isfinite(principal.expires_at)
            or principal.expires_at <= time.time()
        ):
            raise ValueError("a matching issuer and future expiry are required")
        value = asdict(principal)
        value["scopes"] = sorted(principal.scopes)
        token = secrets.token_urlsafe(32)
        saved = await self.store.create(
            self._key(token),
            {"principal": value, "resource": self.resource},
            ttl_seconds=max(1, math.ceil(principal.expires_at - time.time())),
        )
        if not saved:
            raise RuntimeError("the token store refused a fresh identifier")
        return token

    async def verify(self, token: str) -> Principal | None:
        """Return the stored principal, or None for invalid or expired tokens."""
        if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
            return None
        record = await self.store.get(self._key(token))
        if record is None or record.data.get("resource") != self.resource:
            return None
        value = dict(record.data["principal"])
        value["scopes"] = frozenset(value["scopes"])
        principal = Principal(**value)
        if principal.issuer != self.issuer or principal.expired:
            return None
        return principal

    async def revoke(self, token: str) -> None:
        """Delete one token without affecting other grants."""
        await self.store.delete(self._key(token))


class AbstractCipher(ABC):
    """Encode dictionaries to authenticated bytes; reject invalid blobs with ValueError."""

    @abstractmethod
    def encode(self, data: dict) -> bytes: ...

    @abstractmethod
    def decode(self, blob: bytes) -> dict: ...


class KECCAKCipher(AbstractCipher):
    """Compress JSON with zlib, then encrypt with SHAKE-256 XOR and HMAC-SHA256.

    Format: nonce (16 bytes) || ciphertext || MAC (32 bytes). Verify MAC first.
    Anyone with the key can read and create tokens.

    Level 9 favors smaller tokens. On M1 Pro/Python 3.14, encode/decode medians
    were ~35/18 us for 2 KiB and ~283/105 us for 16 KiB of random URL-safe JSON.
    At 16 KiB, level 9 added ~20 us per encode versus level 1; decode was similar.

    Background on SHAKE/Keccak authenticated encryption:
    https://eprint.iacr.org/2024/1618 (Shaking up authenticated encryption)
    https://keccak.team/files/SpongeDuplex.pdf (SpongeWrap)
    https://github.com/XKCP/XKCP (reference impl - ShakingUpAE)
    """

    def __init__(self, key: bytes, compression_level: int = 9) -> None:
        if not isinstance(key, bytes) or len(key) < 32:
            raise ValueError("key must contain at least 32 random bytes")
        self.__key = key
        self.__compression_level = compression_level

    def dump_json(self, data: dict) -> bytes:
        return json.dumps(data, separators=(",", ":")).encode()

    def load_json(self, blob: bytes) -> dict:
        return json.loads(blob)

    def encode(self, data: dict) -> bytes:
        nonce = os.urandom(16)
        compressed = zlib.compress(self.dump_json(data), level=self.__compression_level)
        stream = hashlib.shake_256(self.__key + nonce).digest(len(compressed))
        ct = (int.from_bytes(compressed, "big") ^ int.from_bytes(stream, "big")).to_bytes(
            len(compressed), "big"
        )
        tag = hmac.new(self.__key, nonce + ct, hashlib.sha256).digest()
        return nonce + ct + tag

    def decode(self, blob: bytes) -> dict:
        if len(blob) < 48:
            raise ValueError("bad MAC")
        nonce, ct, tag = blob[:16], blob[16:-32], blob[-32:]
        if not hmac.compare_digest(tag, hmac.new(self.__key, nonce + ct, hashlib.sha256).digest()):
            raise ValueError("bad MAC")
        stream = hashlib.shake_256(self.__key + nonce).digest(len(ct))
        try:
            compressed = (int.from_bytes(ct, "big") ^ int.from_bytes(stream, "big")).to_bytes(
                len(ct), "big"
            )
            return self.load_json(zlib.decompress(compressed))
        except (ValueError, zlib.error):
            raise ValueError("invalid payload") from None


class EncryptedTokens:
    """OAuth payloads encrypted by an application-supplied cipher, in URL-safe base64.

    Pass this object as OAuthServer(tokens=...). Share the cipher configuration
    and the same issuer/resource across instances. No token database is
    needed. Individual revocation is unsupported; replacing the key invalidates
    all tokens. Provider credentials remain separate from Principal.
    """

    def __init__(self, cipher: AbstractCipher, *, issuer: str, resource: str) -> None:
        if (
            not isinstance(issuer, str)
            or not issuer
            or not isinstance(resource, str)
            or not resource
        ):
            raise ValueError("issuer and resource must be non-empty strings")
        self.cipher = cipher
        self.issuer = issuer
        self.resource = resource

    def _principal(self, payload: dict) -> Principal:
        if payload["resource"] != self.resource or not isinstance(payload["upstream"], dict):
            raise ValueError("invalid token")
        value = dict(payload["principal"])
        scopes = value["scopes"]
        if not isinstance(scopes, list) or any(not isinstance(scope, str) for scope in scopes):
            raise ValueError("invalid token")
        value["scopes"] = frozenset(scopes)
        principal = Principal(**value)
        expiry = principal.expires_at
        if (
            principal.issuer != self.issuer
            or not isinstance(expiry, (int, float))
            or isinstance(expiry, bool)
            or not math.isfinite(expiry)
            or expiry <= time.time()
            or (principal.subject is not None and not isinstance(principal.subject, str))
            or not isinstance(principal.client_id, str)
            or not isinstance(principal.claims, dict)
            or (principal.namespace is not None and not isinstance(principal.namespace, str))
        ):
            raise ValueError("invalid token")
        return principal

    async def issue(self, principal: Principal, upstream: dict) -> str:
        """Encode the principal and complete provider response. A future expiry is required."""
        value = asdict(principal)
        value["scopes"] = sorted(principal.scopes)
        payload = {"resource": self.resource, "principal": value, "upstream": upstream}
        self._principal(payload)
        return urlsafe_b64encode(self.cipher.encode(payload)).decode("ascii")

    def decode(self, token: str) -> dict[str, Any]:
        """Return the verified payload, including upstream credentials, or raise ValueError.

        The cipher checks integrity; this method checks issuer, resource and expiry.
        Use the result only in server code; do not return it to an MCP client or log it.
        """
        try:
            blob = b64decode(token, altchars=b"-_", validate=True)
            if urlsafe_b64encode(blob).decode("ascii") != token:
                raise ValueError("invalid token")
            payload = self.cipher.decode(blob)
            self._principal(payload)
            return payload
        except (ValueError, TypeError, KeyError, OverflowError, RecursionError):
            raise ValueError("invalid token") from None

    async def verify(self, token: str) -> Principal | None:
        """Return only the principal. Invalid or expired tokens return None."""
        try:
            return self._principal(self.decode(token))
        except ValueError:
            return None
