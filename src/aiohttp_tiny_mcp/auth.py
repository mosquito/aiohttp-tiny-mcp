"""Extensible HTTP authentication, Basic credentials, and OAuth Bearer tokens."""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from base64 import b64decode
from binascii import Error as Base64Error
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from secrets import compare_digest
from typing import Any, Protocol, runtime_checkable
from urllib.parse import urlsplit

from aiohttp import web

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
    namespace: str | None = None

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


class Authentication(ABC):
    """Subclass this policy and pass an instance to `Registry(auth=...)`.

    Implement `authenticate` and `challenge`. Return a verified `Principal`
    or `None` for invalid credentials. Raise `Unauthorized` for a specific
    refusal. HTTP endpoints enforce expiry, required scopes, session ownership,
    and namespaces after authentication. Policies publish no metadata by default.
    """

    bind_sessions: bool = True
    namespace_from_token: bool = True
    required_scopes: Sequence[str] = ()

    def __init__(
        self,
        *,
        bind_sessions: bool = True,
        namespace_from_token: bool = True,
        required_scopes: Sequence[str] = (),
    ) -> None:
        self.bind_sessions = bind_sessions
        self.namespace_from_token = namespace_from_token
        self.required_scopes = tuple(required_scopes)

    @abstractmethod
    async def authenticate(self, request: web.Request) -> Principal | None:
        """Verify this HTTP request. The full request is available to the policy."""

    @abstractmethod
    def challenge(self, refusal: Unauthorized) -> str:
        """Return the WWW-Authenticate value for this policy."""

    def check(self, principal: Principal | None) -> Principal:
        """Reject invalid identities and missing server-wide scopes."""
        if principal is None or principal.expired:
            raise Unauthorized("invalid_credentials", "valid credentials required")
        missing = principal.holds(self.required_scopes)
        if missing:
            raise Unauthorized(
                "insufficient_scope",
                f"missing scope: {', '.join(sorted(missing))}",
                status=403,
            )
        return principal

    @property
    def metadata_path(self) -> str | None:
        """Return an optional metadata route, relative to the site root."""
        return None

    def metadata(self) -> dict[str, Any]:
        """Return metadata when the policy declares a metadata path."""
        return {}


class BasicAuth(Authentication):
    """Subclass `verify` to authenticate Basic credentials against your user store.

    Credentials use standard Base64 and UTF-8. Query credentials are not read.
    Use `StaticBasicAuth` for one configured username and password.
    """

    def __init__(
        self,
        *,
        realm: str = "mcp",
        bind_sessions: bool = True,
        namespace_from_token: bool = True,
        required_scopes: Sequence[str] = (),
    ) -> None:
        super().__init__(
            bind_sessions=bind_sessions,
            namespace_from_token=namespace_from_token,
            required_scopes=required_scopes,
        )
        if not realm.isascii() or any(ord(char) < 32 or ord(char) == 127 for char in realm):
            raise ValueError("realm must contain only printable ASCII characters")
        self.realm = realm

    @abstractmethod
    async def verify(self, username: str, password: str) -> Principal | None:
        """Return the verified identity, or None when credentials do not match."""

    async def authenticate(self, request: web.Request) -> Principal | None:
        header = request.headers.get("Authorization", "")
        parts = header.split()
        if len(parts) != 2 or parts[0].lower() != "basic":
            return None
        try:
            decoded = b64decode(parts[1], validate=True).decode("utf-8")
        except (ValueError, Base64Error, UnicodeDecodeError):
            return None
        username, separator, password = decoded.partition(":")
        if not separator or any(ord(char) < 32 or ord(char) == 127 for char in decoded):
            return None
        return await self.verify(username, password)

    def challenge(self, refusal: Unauthorized) -> str:
        realm = self.realm.replace("\\", "\\\\").replace('"', '\\"')
        return f'Basic realm="{realm}", charset="UTF-8"'


class StaticBasicAuth(BasicAuth):
    """Basic authentication for one configured account. Use HTTPS in production."""

    def __init__(
        self,
        username: str,
        password: str,
        *,
        realm: str = "mcp",
        scopes: Iterable[str] = (),
        bind_sessions: bool = True,
        namespace_from_token: bool = True,
        required_scopes: Sequence[str] = (),
    ) -> None:
        super().__init__(
            realm=realm,
            bind_sessions=bind_sessions,
            namespace_from_token=namespace_from_token,
            required_scopes=required_scopes,
        )
        if ":" in username or any(
            ord(char) < 32 or ord(char) == 127 for char in username + password
        ):
            raise ValueError("Basic credentials cannot contain controls or a colon in the username")
        if not username or not password:
            raise ValueError("username and password must be non-empty")
        self._username = username.encode("utf-8")
        self._password = password.encode("utf-8")
        self._principal = Principal(subject=username, scopes=frozenset(scopes))

    async def verify(self, username: str, password: str) -> Principal | None:
        matched = compare_digest(username.encode("utf-8"), self._username)
        matched &= compare_digest(password.encode("utf-8"), self._password)
        return self._principal if matched else None


@dataclass(frozen=True)
class Authorization(Authentication):
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
        return self.check(found)

    async def authenticate(self, request: web.Request) -> Principal:
        """Verify the request's Bearer header using the configured token verifier."""
        return await self.principal(request.headers.get("Authorization"))


__all__ = [
    "WELL_KNOWN",
    "Authentication",
    "Authorization",
    "BasicAuth",
    "Principal",
    "StaticBasicAuth",
    "TokenVerifier",
    "Unauthorized",
]
