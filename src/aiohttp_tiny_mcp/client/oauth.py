"""Provider-independent OAuth for HTTP MCP clients.

Applications own user interaction and durable storage. Each instance owns one
resource/account context and serializes refresh within that context only.
"""

from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import hmac
import json
import math
import re
import secrets
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl, quote_plus, urlsplit

import aiohttp
from yarl import URL

from ._oauth import (
    AuthorizationRequired,
    OAuthError,
    RegistrationRequired,
    bearer_challenge,
    checked_url,
    strings,
)
from ._oauth import (
    scopes as parse_scopes,
)

Credentials = dict[str, Any]
LoadCredentials = Callable[[], Awaitable[Credentials | None]]
SaveCredentials = Callable[[Credentials], Awaitable[None]]
Authorize = Callable[[str], Awaitable[str]]
CheckURL = Callable[[str], Awaitable[None]]

MAX_RESPONSE = 1024 * 1024
_AUTH_METHODS = {"none", "client_secret_basic", "client_secret_post"}


class _MissingMetadata(OAuthError):
    pass


class _TokenError(OAuthError):
    pass


@dataclass(frozen=True, eq=False, repr=False)
class AuthorizationAttempt:
    """One pending login. Only its authorization URL is public."""

    url: str


@dataclass(repr=False)
class _Pending:
    state: str
    verifier: str
    deadline: float
    scopes: tuple[str, ...]


class OAuth:
    """OAuth authorization for one MCP endpoint and one application account.

    Use as an async context manager, then pass it to ``Client(auth=...)``.
    ``login`` explicitly starts user interaction; ordinary requests only refresh.
    ``load`` and ``save`` are application callbacks scoped to this account and
    connection. Shared credentials require coordination outside this instance.

    HTTPS is required. ``allow_http`` permits loopback HTTP for development.
    ``check_url`` can enforce an application's network policy before each fetch
    and before returning an authorization URL to the application.
    """

    def __init__(
        self,
        endpoint: str,
        *,
        redirect_uri: str,
        issuer: str | None = None,
        client_id: str | None = None,
        client_secret: str | None = None,
        token_endpoint_auth_method: str = "none",
        metadata_url: str | None = None,
        registration: Mapping[str, Any] | None = None,
        scopes: Sequence[str] | None = None,
        load: LoadCredentials | None = None,
        save: SaveCredentials | None = None,
        allow_http: bool = False,
        check_url: CheckURL | None = None,
        timeout: float = 30,
        login_timeout: float = 300,
    ) -> None:
        self.endpoint = str(checked_url(endpoint, allow_http=allow_http))
        self.redirect_uri = str(checked_url(redirect_uri, allow_http=True))
        if URL(self.redirect_uri).query:
            raise ValueError("The redirect URI must not contain a query string.")
        if client_id is not None and (not client_id or issuer is None):
            raise ValueError("A static client ID requires its registered issuer.")
        if issuer is not None:
            if checked_url(issuer, allow_http=allow_http).query:
                raise ValueError("The issuer must not contain a query string.")
        if token_endpoint_auth_method not in _AUTH_METHODS:
            raise ValueError("Unsupported token endpoint authentication method.")
        if client_secret is not None and (not client_id or token_endpoint_auth_method == "none"):
            raise ValueError("A client secret requires a static confidential client.")
        if client_id and token_endpoint_auth_method != "none" and not client_secret:
            raise ValueError("The selected authentication method requires a client secret.")
        if metadata_url is not None:
            url = checked_url(metadata_url)
            if url.path == "/" or url.query:
                raise ValueError("A client metadata URL must have a path and no query.")
        if (load is None) != (save is None):
            raise ValueError("Supply both load and save callbacks, or neither.")
        if not all(math.isfinite(t) and t > 0 for t in (timeout, login_timeout)):
            raise ValueError("OAuth timeouts must be positive.")
        self._expected_issuer = issuer
        self._static_id = client_id
        self._static_secret = client_secret
        self._auth_method = token_endpoint_auth_method
        self._metadata_url = metadata_url
        self._registration = copy.deepcopy(dict(registration)) if registration is not None else None
        self._requested = None if scopes is None else parse_scopes(" ".join(scopes))
        self._load = load
        self._save = save
        self._allow_http = allow_http
        self._check_url = check_url
        self._timeout = timeout
        self._login_timeout = login_timeout
        self._session: aiohttp.ClientSession | None = None
        self._lock = asyncio.Lock()
        self._issuer: str | None = None
        self._resource: str | None = None
        self._server: dict[str, Any] = {}
        self._challenge: dict[str, str] = {}
        self._scope: set[str] = set(self._requested or ())
        self._record: Credentials | None = None
        self._loaded = False
        self._save_failed = False
        self._pending: dict[AuthorizationAttempt, _Pending] = {}

    async def __aenter__(self) -> OAuth:
        if self._session is not None:
            raise RuntimeError("The OAuth context is already open.")
        self._session = aiohttp.ClientSession(
            cookie_jar=aiohttp.DummyCookieJar(),
            timeout=aiohttp.ClientTimeout(total=self._timeout),
            trust_env=False,
        )
        self._server = {}
        return self

    async def __aexit__(self, *exc: object) -> None:
        self._pending.clear()
        if self._session is not None:
            await self._session.close()
            self._session = None

    def _http(self) -> aiohttp.ClientSession:
        if self._session is None:
            raise RuntimeError("Use 'async with OAuth(...) as auth:'.")
        return self._session

    async def _url(self, value: object) -> str:
        url = str(checked_url(value, allow_http=self._allow_http))
        if self._check_url is not None:
            await self._check_url(url)
        return url

    def check_endpoint(self, endpoint: str) -> None:
        if URL(endpoint) != URL(self.endpoint):
            raise ValueError("The OAuth context belongs to another MCP endpoint.")

    def observe(self, headers: Iterable[str]) -> dict[str, str]:
        """Remember a server challenge without starting user interaction."""
        challenge = bearer_challenge(headers)
        if "resource_metadata" in challenge:
            checked_url(challenge["resource_metadata"], allow_http=self._allow_http)
        if "scope" in challenge:
            self._scope.update(parse_scopes(challenge["scope"]))
        self._challenge.update(challenge)
        return challenge

    async def _json(
        self,
        method: str,
        url: str,
        *,
        metadata: bool = False,
        headers: Mapping[str, str] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        url = await self._url(url)
        try:
            async with self._http().request(
                method,
                url,
                headers={"Accept": "application/json", **(headers or {})},
                allow_redirects=False,
                **kwargs,
            ) as response:
                if metadata and response.status in {404, 405}:
                    raise _MissingMetadata("OAuth metadata is not available at this location.")
                body = bytearray()
                async for chunk in response.content.iter_chunked(16384):
                    body.extend(chunk)
                    if len(body) > MAX_RESPONSE:
                        raise OAuthError("OAuth response is too large.")
                try:
                    data = json.loads(body)
                except (ValueError, UnicodeError):
                    raise OAuthError("Invalid OAuth JSON response.") from None
                if not isinstance(data, dict):
                    raise OAuthError("Invalid OAuth JSON response.")
                if response.status >= 400 or "error" in data:
                    # Do not expose error_description, response bodies, or URLs with secrets.
                    raise _TokenError("The OAuth server rejected the request.")
                if response.status < 200 or response.status >= 300:
                    raise OAuthError("OAuth redirects are not allowed.")
                return data
        except (aiohttp.ClientError, TimeoutError, asyncio.TimeoutError):
            raise OAuthError("The OAuth HTTP request failed.") from None

    async def _discover(self, *, force: bool = False) -> None:
        self._http()
        if self._server and not force:
            await self._restore()
            return
        endpoint = checked_url(self.endpoint, allow_http=self._allow_http)
        if "resource_metadata" not in self._challenge:
            try:
                async with self._http().get(
                    await self._url(self.endpoint), allow_redirects=False
                ) as response:
                    if response.status == 401:
                        self.observe(response.headers.getall("WWW-Authenticate", []))
            except (aiohttp.ClientError, TimeoutError, asyncio.TimeoutError):
                raise OAuthError("The MCP discovery request failed.") from None
        metadata_uri = self._challenge.get("resource_metadata")
        origin = str(endpoint.origin())
        locations = (
            [metadata_uri]
            if metadata_uri
            else list(
                dict.fromkeys(
                    [
                        origin
                        + "/.well-known/oauth-protected-resource"
                        + endpoint.raw_path.rstrip("/"),
                        origin + "/.well-known/oauth-protected-resource",
                    ]
                )
            )
        )
        resource: dict[str, Any] | None = None
        for location in locations:
            try:
                resource = await self._json("GET", location, metadata=True)
                break
            except _MissingMetadata:
                continue
        if resource is None:
            raise OAuthError("The MCP server does not provide protected resource metadata.")
        resource_url = checked_url(resource.get("resource"), allow_http=self._allow_http)
        # Metadata can identify the exact endpoint, or omit transport query parameters.
        if resource_url not in {endpoint, endpoint.with_query(None)}:
            raise OAuthError("OAuth metadata describes another MCP resource.")
        issuers = strings(resource.get("authorization_servers"))
        selected = self._expected_issuer or self._issuer
        if selected is None:
            if len(issuers) != 1:
                raise OAuthError(
                    "Select an issuer explicitly when multiple authorization servers exist."
                )
            selected = issuers[0]
        if selected not in issuers:
            raise OAuthError("The registered issuer is not advertised by this MCP server.")
        issuer_url = checked_url(selected, allow_http=self._allow_http)
        if issuer_url.query:
            raise OAuthError("The issuer must not contain a query string.")
        issuer_origin = str(issuer_url.origin())
        path = issuer_url.raw_path.rstrip("/")
        locations = list(
            dict.fromkeys(
                [
                    issuer_origin + "/.well-known/oauth-authorization-server" + path,
                    issuer_origin + "/.well-known/openid-configuration" + path,
                    str(issuer_url).rstrip("/") + "/.well-known/openid-configuration",
                ]
            )
        )
        server: dict[str, Any] | None = None
        for location in locations:
            try:
                server = await self._json("GET", location, metadata=True)
                break
            except _MissingMetadata:
                continue
        if server is None or server.get("issuer") != selected:
            raise OAuthError("Authorization server metadata has no matching issuer.")
        if "S256" not in strings(server.get("code_challenge_methods_supported", [])):
            raise OAuthError("The authorization server must advertise PKCE S256.")
        if "code" not in strings(server.get("response_types_supported", [])):
            raise OAuthError("The authorization server does not support authorization codes.")
        for key in ("authorization_endpoint", "token_endpoint"):
            await self._url(server.get(key))
        self._server = server
        self._issuer = selected
        self._resource = str(resource_url)
        if not self._scope:
            self._scope.update(
                self._requested
                if self._requested is not None
                else (
                    parse_scopes(self._challenge["scope"])
                    if "scope" in self._challenge
                    else parse_scopes(" ".join(strings(resource.get("scopes_supported", []))))
                )
            )
        await self._restore()

    async def _restore(self) -> None:
        if self._loaded:
            return
        record = await self._load() if self._load else None
        if record is not None:
            if not isinstance(record, dict) or record.get("version") != 1:
                raise OAuthError("Unsupported stored OAuth credentials.")
            if record.get("issuer") != self._issuer or record.get("resource") != self._resource:
                raise OAuthError("Stored OAuth credentials belong to another issuer or resource.")
            registration = record.get("registration")
            if not isinstance(registration, dict) or not registration.get("client_id"):
                raise OAuthError("Stored OAuth credentials have no client registration.")
            source = record.get("registration_source")
            if source == "static":
                expected = self._static_id
            elif source == "cimd":
                expected = self._metadata_url
            elif source == "dcr" and self._registration is not None and not self._static_id:
                expected = registration["client_id"]
            else:
                expected = None
            if not expected or registration["client_id"] != expected:
                raise OAuthError("Stored OAuth credentials belong to another client.")
            if record.get("client_id") != registration["client_id"]:
                raise OAuthError("Stored OAuth client bindings do not match.")
            self._validate_registration(registration)
            tokens = record.get("tokens")
            if not isinstance(tokens, dict):
                raise OAuthError("Invalid stored OAuth token response.")
            if tokens:
                self._validate_tokens(tokens)
            if "expires_at" not in record:
                raise OAuthError("Stored OAuth credentials have no expiry field.")
            expires = record["expires_at"]
            if expires is not None and (
                type(expires) not in {int, float} or not math.isfinite(expires)
            ):
                raise OAuthError("Invalid stored OAuth expiry.")
            self._scope.update(parse_scopes(record.get("requested_scope", "")))
            self._record = copy.deepcopy(record)
        self._loaded = True

    async def _write(self, record: Credentials) -> None:
        self._record = copy.deepcopy(record)
        self._save_failed = True
        if self._save:
            await self._save(copy.deepcopy(record))
        self._save_failed = False

    def _validate_registration(self, registration: dict[str, Any]) -> None:
        client_id = registration.get("client_id")
        if not isinstance(client_id, str) or not client_id or len(client_id) > 4096:
            raise OAuthError("Invalid OAuth client ID.")
        method = registration.get("token_endpoint_auth_method", "client_secret_basic")
        if not isinstance(method, str) or method not in _AUTH_METHODS:
            raise OAuthError("Unsupported client authentication method.")
        if method not in strings(
            self._server.get("token_endpoint_auth_methods_supported", ["client_secret_basic"])
        ):
            raise OAuthError(
                "The authorization server does not support the client authentication method."
            )
        if method != "none" and (
            not isinstance(registration.get("client_secret"), str)
            or not registration["client_secret"]
        ):
            raise OAuthError("The registered client has no secret.")
        redirects = strings(registration.get("redirect_uris", []))
        if self.redirect_uri not in redirects:
            raise OAuthError("The callback URI is not registered for this client.")
        expiry = registration.get("client_secret_expires_at", 0)
        if type(expiry) not in {int, float} or not math.isfinite(expiry) or expiry < 0:
            raise OAuthError("Invalid client secret expiry.")
        if expiry and expiry <= time.time():
            raise RegistrationRequired("The client secret has expired.")

    async def _register(self) -> None:
        if self._record is not None:
            self._validate_registration(self._record["registration"])
            if self._save_failed:
                await self._write(self._record)
            return
        if self._static_id:
            source = "static"
            registration = {
                "client_id": self._static_id,
                "token_endpoint_auth_method": self._auth_method,
                "redirect_uris": [self.redirect_uri],
            }
            if self._static_secret:
                registration["client_secret"] = self._static_secret
        elif (
            self._metadata_url and self._server.get("client_id_metadata_document_supported") is True
        ):
            source = "cimd"
            registration = {
                "client_id": self._metadata_url,
                "token_endpoint_auth_method": "none",
                "redirect_uris": [self.redirect_uri],
            }
        elif self._registration is not None and self._server.get("registration_endpoint"):
            source = "dcr"
            metadata = {
                "client_name": "aiohttp-tiny-mcp",
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "token_endpoint_auth_method": "none",
                "application_type": "native" if URL(self.redirect_uri).scheme == "http" else "web",
                **self._registration,
                "redirect_uris": [self.redirect_uri],
            }
            registration = await self._json(
                "POST", self._server["registration_endpoint"], json=metadata
            )
            if (
                registration.get("token_endpoint_auth_method", "client_secret_basic")
                != metadata["token_endpoint_auth_method"]
            ):
                raise OAuthError("Registration returned a different client authentication method.")
        else:
            raise RegistrationRequired("Supply a static client, a CIMD URL, or DCR metadata.")
        self._validate_registration(registration)
        await self._write(
            {
                "version": 1,
                "issuer": self._issuer,
                "resource": self._resource,
                "client_id": registration["client_id"],
                "registration": registration,
                "registration_source": source,
                "tokens": {},
                "expires_at": None,
                "requested_scope": " ".join(sorted(self._scope)),
            }
        )

    async def begin(self, *, scopes: Sequence[str] | None = None) -> AuthorizationAttempt:
        """Start a login, replacing any pending login in this context."""
        async with self._lock:
            await self._discover()
            await self._register()
            if scopes is not None:
                self._scope.update(parse_scopes(" ".join(scopes)))
            state = secrets.token_urlsafe(32)
            verifier = secrets.token_urlsafe(32)
            challenge = (
                base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
                .rstrip(b"=")
                .decode()
            )
            assert self._record is not None and self._resource is not None
            params = {
                "response_type": "code",
                "client_id": self._record["client_id"],
                "redirect_uri": self.redirect_uri,
                "resource": self._resource,
                "state": state,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            }
            if self._scope:
                params["scope"] = " ".join(sorted(self._scope))
            url = str(
                URL(await self._url(self._server["authorization_endpoint"])).update_query(params)
            )
            attempt = AuthorizationAttempt(url)
            self._pending.clear()
            self._pending[attempt] = _Pending(
                state, verifier, time.monotonic() + self._login_timeout, tuple(self._scope)
            )
            return attempt

    def cancel(self, attempt: AuthorizationAttempt) -> None:
        """Discard a pending login without changing existing credentials."""
        self._pending.pop(attempt, None)

    async def login(self, *, authorize: Authorize, scopes: Sequence[str] | None = None) -> None:
        """Run an explicit login through the application's browser/callback handler."""
        attempt = await self.begin(scopes=scopes)
        try:
            response = await asyncio.wait_for(authorize(attempt.url), timeout=self._login_timeout)
            await self.complete(attempt, response)
        finally:
            self.cancel(attempt)

    async def complete(self, attempt: AuthorizationAttempt, callback_url: str) -> None:
        """Validate and consume a callback before sending its code to the token endpoint."""
        async with self._lock:
            self._http()
            pending = self._pending.pop(attempt, None)
            if pending is None or pending.deadline <= time.monotonic():
                raise OAuthError("The authorization attempt expired or was already consumed.")
            callback = checked_url(callback_url, allow_http=True)
            if callback.with_query(None) != URL(self.redirect_uri):
                raise OAuthError("The authorization callback URI does not match.")
            if len(callback_url) > 16384:
                raise OAuthError("The authorization response is too large.")
            try:
                pairs = parse_qsl(
                    urlsplit(callback_url).query, keep_blank_values=True, errors="strict"
                )
            except (ValueError, UnicodeError):
                raise OAuthError("Invalid authorization response encoding.") from None
            response = dict(pairs)
            if len(response) != len(pairs):
                raise OAuthError("Duplicate authorization response parameters.")
            if not hmac.compare_digest(response.get("state", "").encode(), pending.state.encode()):
                raise OAuthError("The authorization state does not match.")
            issuer = response.get("iss")
            if (issuer is not None and issuer != self._issuer) or (
                issuer is None
                and self._server.get("authorization_response_iss_parameter_supported") is True
            ):
                raise OAuthError("The authorization response issuer does not match.")
            if "error" in response or not response.get("code"):
                raise OAuthError("Authorization was refused or returned no code.")
            tokens = await self._exchange(
                {
                    "grant_type": "authorization_code",
                    "code": response["code"],
                    "code_verifier": pending.verifier,
                    "redirect_uri": self.redirect_uri,
                }
            )
            await self._store_tokens(tokens, pending.scopes)

    async def _exchange(self, form: dict[str, str]) -> dict[str, Any]:
        assert self._record is not None and self._resource is not None
        registration = self._record["registration"]
        self._validate_registration(registration)
        form = {**form, "resource": self._resource, "client_id": registration["client_id"]}
        method = registration.get("token_endpoint_auth_method", "client_secret_basic")
        headers = {}
        if method == "client_secret_post":
            form["client_secret"] = registration["client_secret"]
        elif method == "client_secret_basic":
            headers["Authorization"] = aiohttp.encode_basic_auth(
                quote_plus(registration["client_id"]), quote_plus(registration["client_secret"])
            )
        return await self._json("POST", self._server["token_endpoint"], data=form, headers=headers)

    @staticmethod
    def _validate_tokens(tokens: dict[str, Any]) -> None:
        access = tokens.get("access_token")
        if (
            not isinstance(access, str)
            or len(access) > 8192
            or not re.fullmatch(r"[A-Za-z0-9._~+/\-]+=*", access)
        ):
            raise OAuthError("The token response has no valid access token.")
        if str(tokens.get("token_type", "")).lower() != "bearer":
            raise OAuthError("The token response must contain a Bearer token.")
        expiry = tokens.get("expires_in")
        if "expires_in" in tokens and (
            expiry is None
            or type(expiry) not in {int, float}
            or not 0 <= expiry <= 10**12
            or not math.isfinite(expiry)
        ):
            raise OAuthError("Invalid access token lifetime.")
        refresh = tokens.get("refresh_token")
        if "refresh_token" in tokens and (
            not isinstance(refresh, str) or not refresh or len(refresh) > 8192
        ):
            raise OAuthError("Invalid refresh token.")
        if "scope" in tokens:
            parse_scopes(tokens["scope"])

    async def _store_tokens(self, tokens: dict[str, Any], requested: Iterable[str]) -> None:
        self._validate_tokens(tokens)
        assert self._record is not None
        expires = tokens.get("expires_in")
        await self._write(
            {
                **self._record,
                "tokens": tokens,
                "expires_at": None if expires is None else time.time() + expires,
                "requested_scope": " ".join(sorted(set(requested))),
            }
        )

    async def token(self, *, rejected: str | None = None) -> str:
        """Return a usable token, refreshing once if needed. Never start a login."""
        async with self._lock:
            await self._discover()
            if self._save_failed:
                raise OAuthError("OAuth credentials were not saved; explicit login is required.")
            if self._record is None or not self._record["tokens"]:
                raise AuthorizationRequired(self._scope)
            tokens = self._record["tokens"]
            expires = self._record["expires_at"]
            access = tokens["access_token"]
            skew = min(30, tokens.get("expires_in", 300) / 10)
            if access != rejected and (expires is None or expires > time.time() + skew):
                return access
            if access == rejected:
                await self._discover(force=True)
            refresh = tokens.get("refresh_token")
            if not refresh:
                await self._write({**self._record, "tokens": {}, "expires_at": None})
                raise AuthorizationRequired(self._scope)
            # Reserve this refresh before sending it. A timeout or cancellation can
            # mean that the server rotated it. Never retry it on the next request.
            requested = self._record["requested_scope"]
            await self._write({**self._record, "tokens": {}, "expires_at": None})
            try:
                updated = await self._exchange(
                    {"grant_type": "refresh_token", "refresh_token": refresh}
                )
            except _TokenError:
                raise AuthorizationRequired(self._scope) from None
            if "refresh_token" not in updated:
                updated["refresh_token"] = refresh
            await self._store_tokens(updated, parse_scopes(requested))
            if updated.get("expires_in") == 0:
                raise AuthorizationRequired(self._scope)
            return updated["access_token"]

    async def invalidate(self, rejected: str) -> None:
        """Discard a repeatedly rejected token without erasing a concurrent replacement."""
        async with self._lock:
            if self._record and self._record["tokens"].get("access_token") == rejected:
                await self._write({**self._record, "tokens": {}, "expires_at": None})

    def authorization_required(self) -> AuthorizationRequired:
        return AuthorizationRequired(self._scope)


__all__ = [
    "AuthorizationAttempt",
    "AuthorizationRequired",
    "Authorize",
    "Credentials",
    "LoadCredentials",
    "OAuth",
    "OAuthError",
    "RegistrationRequired",
    "SaveCredentials",
]
