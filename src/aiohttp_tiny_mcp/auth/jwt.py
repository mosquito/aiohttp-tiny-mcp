"""Optional JWT verification. Install aiohttp-tiny-mcp[jwt] to use this module."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import jwt
    from cryptography.exceptions import UnsupportedAlgorithm
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec, ed448, ed25519, rsa
    from jwt.algorithms import HMACAlgorithm
except ImportError as error:
    raise ImportError("JWT verification requires aiohttp-tiny-mcp[jwt]") from error

from aiohttp_tiny_mcp.auth import Principal, principal_from_claims

VerificationKey = (
    bytes
    | rsa.RSAPublicKey
    | ec.EllipticCurvePublicKey
    | ed25519.Ed25519PublicKey
    | ed448.Ed448PublicKey
)


@dataclass(frozen=True)
class JWTVerifier:
    """Verify JWTs, then map verified claims with the synchronous principal hook.

    Use HMACJWTVerifier or PublicKeyJWTVerifier to select algorithms from trusted
    key material. The mapper may raise ValueError to reject verified claims.
    """

    key: VerificationKey = field(repr=False)
    algorithms: tuple[str, ...]
    issuer: str = ""
    audience: str = ""
    principal: Callable[[Mapping[str, Any]], Principal] = principal_from_claims

    async def verify(self, credentials: str) -> Principal | None:
        """Check the signature, required expiry, and configured issuer and audience."""
        try:
            claims = jwt.decode(
                credentials,
                self.key,
                algorithms=self.algorithms,
                issuer=self.issuer or None,
                audience=self.audience or None,
                options={"require": ["exp"], "verify_aud": bool(self.audience)},
            )
            return self.principal(claims)
        except (jwt.InvalidTokenError, ValueError, TypeError, OverflowError):
            return None


class HMACJWTVerifier(JWTVerifier):
    """Verify HS256 JWTs with a configured pre-shared key."""

    def __init__(
        self,
        pre_shared_key: str,
        *,
        issuer: str = "",
        audience: str = "",
        principal: Callable[[Mapping[str, Any]], Principal] = principal_from_claims,
    ) -> None:
        hmac = HMACAlgorithm(HMACAlgorithm.SHA256)
        try:
            key = hmac.prepare_key(pre_shared_key)
        except jwt.InvalidKeyError as error:
            raise ValueError("the JWT pre-shared key must not be an asymmetric key") from error
        if len(key) < 32:
            raise ValueError("the JWT pre-shared key must contain at least 32 bytes")
        super().__init__(key, ("HS256",), issuer, audience, principal)


class PublicKeyJWTVerifier(JWTVerifier):
    """Verify RSA, EC, or EdDSA JWTs with a configured public key."""

    def __init__(
        self,
        public_key: str,
        *,
        issuer: str = "",
        audience: str = "",
        principal: Callable[[Mapping[str, Any]], Principal] = principal_from_claims,
    ) -> None:
        material = public_key.strip()
        if not material.startswith("-----BEGIN "):
            material = Path(material).expanduser().read_text(encoding="utf-8")
        try:
            loaded = serialization.load_pem_public_key(material.encode("utf-8"))
        except (ValueError, TypeError, UnsupportedAlgorithm) as error:
            raise ValueError("the JWT public key must be a supported PEM public key") from error
        algorithms: tuple[str, ...]
        if isinstance(loaded, rsa.RSAPublicKey):
            if loaded.key_size < 2048:
                raise ValueError("the JWT RSA public key must contain at least 2048 bits")
            algorithms = ("RS256", "RS384", "RS512", "PS256", "PS384", "PS512")
        elif isinstance(loaded, ec.EllipticCurvePublicKey):
            curves = {"secp256r1": "ES256", "secp384r1": "ES384", "secp521r1": "ES512"}
            algorithm = curves.get(loaded.curve.name)
            if algorithm is None:
                raise ValueError("unsupported JWT elliptic curve")
            algorithms = (algorithm,)
        elif isinstance(loaded, (ed25519.Ed25519PublicKey, ed448.Ed448PublicKey)):
            algorithms = ("EdDSA",)
        else:
            raise ValueError("unsupported JWT public key type")
        super().__init__(loaded, algorithms, issuer, audience, principal)


__all__ = ["HMACJWTVerifier", "JWTVerifier", "PublicKeyJWTVerifier"]
