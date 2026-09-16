"""Bearer-token verification for an OAuth protected MCP resource."""

from __future__ import annotations

import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable
from urllib.parse import urlsplit

WELL_KNOWN = "/.well-known/oauth-protected-resource"


@dataclass(frozen=True, slots=True)
class Principal:
    """Identity and claims returned by a token verifier."""

    subject: str | None = None
    client_id: str = ""
    issuer: str | None = None
    scopes: frozenset[str] = frozenset()
    expires_at: float | None = None
    claims: Mapping[str, Any] = field(default_factory=dict)

    def holds(self, wanted: Iterable[str]) -> frozenset[str]:
        """Return required scopes absent from this principal."""
        return frozenset(wanted) - self.scopes

    @property
    def expired(self) -> bool:
        return self.expires_at is not None and self.expires_at < time.time()

    @property
    def identity(self) -> str:
        """Return an issuer-qualified subject or client identifier."""
        if self.subject:
            return f"{self.issuer or ''}|{self.subject}"
        return f"{self.issuer or ''}|client:{self.client_id}"


@runtime_checkable
class TokenVerifier(Protocol):
    """Verify a bearer token for this resource."""

    async def verify(self, token: str) -> Principal | None: ...


class Unauthorized(Exception):
    """Authentication or authorization failed."""

    def __init__(self, error: str, description: str, status: int = 401) -> None:
        super().__init__(description)
        self.error = error
        self.description = description
        self.status = status


@dataclass(frozen=True)
class Authorization:
    """Configuration for an OAuth protected resource server."""

    verifier: TokenVerifier
    resource: str
    authorization_servers: Sequence[str] = ()
    scopes_supported: Sequence[str] | None = None
    required_scopes: Sequence[str] = ()
    resource_name: str | None = None
    documentation: str | None = None
    bind_sessions: bool = True
    namespace_from_token: bool = True

    @property
    def metadata_path(self) -> str:
        """Return this resource's RFC 9728 metadata path."""
        path = urlsplit(self.resource).path.rstrip("/")
        return f"{WELL_KNOWN}{path}"

    def metadata(self) -> dict[str, Any]:
        """Build this resource's RFC 9728 metadata document."""
        found: dict[str, Any] = {"resource": self.resource}
        if self.authorization_servers:
            found["authorization_servers"] = list(self.authorization_servers)
        if self.scopes_supported is not None:
            found["scopes_supported"] = list(self.scopes_supported)
        if self.resource_name:
            found["resource_name"] = self.resource_name
        if self.documentation:
            found["resource_documentation"] = self.documentation
        found["bearer_methods_supported"] = ["header"]
        return found

    def challenge(self, refusal: Unauthorized) -> str:
        """Build a Bearer challenge with a metadata URL."""
        parts = [
            f'error="{refusal.error}"',
            f'error_description="{refusal.description}"',
            f'resource_metadata="{self.metadata_url}"',
        ]
        return "Bearer " + ", ".join(parts)

    @property
    def metadata_url(self) -> str:
        split = urlsplit(self.resource)
        return f"{split.scheme}://{split.netloc}{self.metadata_path}"

    async def principal(self, authorization: str | None) -> Principal:
        """Verify an Authorization header and return its principal."""
        if not authorization or not authorization.lower().startswith("bearer "):
            raise Unauthorized("invalid_request", "authorization required")
        found = await self.verifier.verify(authorization[len("bearer ") :].strip())
        if found is None or found.expired:
            raise Unauthorized("invalid_token", "the token is not valid for this resource")
        missing = found.holds(self.required_scopes)
        if missing:
            raise Unauthorized(
                "insufficient_scope",
                f"missing scope: {', '.join(sorted(missing))}",
                status=403,
            )
        return found


__all__ = [
    "WELL_KNOWN",
    "Authorization",
    "Principal",
    "TokenVerifier",
    "Unauthorized",
]
