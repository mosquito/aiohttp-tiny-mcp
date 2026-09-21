"""An OAuth authorization-code server for explicitly registered public clients."""

from __future__ import annotations

import hashlib
import inspect
import ipaddress
import json
import re
import secrets
import time
from base64 import urlsafe_b64encode
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from html import escape
from typing import Any

from aiohttp import ClientSession, ClientTimeout, DummyCookieJar, web
from yarl import URL

from ..auth import Authorization, Principal
from ..metadata import metadata_route
from ..sessions import MemorySessionStore, SessionRecord, SessionStore
from .upstream import Identity, OAuth2, UpstreamAuthError

FLOW_TTL = 300
OPAQUE = re.compile(r"[A-Za-z0-9_-]{43}\Z")
VERIFIER = re.compile(r"[A-Za-z0-9._~-]{43,128}\Z")
SCOPE = re.compile(r"[\x21\x23-\x5b\x5d-\x7e]+\Z")
PRIVATE_HEADERS = {
    "Cache-Control": "no-store",
    "Pragma": "no-cache",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
}


def challenge(verifier: str) -> str:
    return (
        urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode()
    )


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def checked_url(value: str, *, query: bool = False) -> URL:
    """Require HTTPS, except for explicit loopback development addresses."""
    url = URL(value)
    host = url.host or ""
    try:
        loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = host == "localhost"
    if (
        not host
        or url.user is not None
        or url.password is not None
        or url.fragment
        or (url.query_string and not query)
        or (url.scheme != "https" and not (url.scheme == "http" and loopback))
        or any(ord(char) <= 32 or ord(char) == 127 for char in value)
    ):
        raise ValueError(
            "OAuth URLs require HTTPS (or HTTP on loopback), without credentials or fragments"
        )
    return url


@dataclass(frozen=True)
class OAuthClient:
    """Register a trusted public client with exact callback URLs.

    Set require_consent to show a local confirmation before provider sign-in.
    Client secrets are not used.
    """

    client_id: str
    redirect_uris: Sequence[str]
    client_name: str = "MCP client"
    require_consent: bool = False


class OAuthFailure(Exception):
    def __init__(self, error: str, description: str, status: int = 400) -> None:
        self.error = error
        self.description = description
        self.status = status


class OAuthFacade:
    """Exchange upstream identity for short-lived MCP tokens.

    This implementation supports authorization_code, PKCE S256, and explicitly
    registered public clients. It does not register clients dynamically or issue
    refresh tokens. Supply a shared SessionStore for multiple workers.
    """

    def __init__(
        self,
        issuer: str,
        upstream: OAuth2,
        *,
        resource: str,
        clients: Sequence[OAuthClient],
        scopes: Sequence[str] = (),
        identity: Callable[[Identity], Principal | None | Awaitable[Principal | None]]
        | None = None,
        store: SessionStore | None = None,
        access_token_ttl: int = 3600,
    ) -> None:
        issuer_url = checked_url(issuer)
        checked_url(resource)
        checked_url(upstream.authorize_url, query=True)
        checked_url(upstream.token_url)
        if issuer.endswith("/") or issuer_url.query_string:
            raise ValueError("issuer must not end in a slash or contain a query")
        if not upstream.client_id or not upstream.client_secret:
            raise ValueError("the upstream client ID and secret are required")
        if type(access_token_ttl) is not int or not 1 <= access_token_ttl <= 86400:
            raise ValueError("access_token_ttl must be between 1 and 86400 seconds")
        if not clients:
            raise ValueError("register at least one OAuth client")
        self.clients: dict[str, OAuthClient] = {}
        for client in clients:
            if not client.client_id or client.client_id in self.clients or not client.redirect_uris:
                raise ValueError("OAuth clients need unique non-empty IDs and callback URLs")
            for uri in client.redirect_uris:
                url = checked_url(uri, query=True)
                if set(url.query) & {"code", "state", "iss", "error", "error_description"}:
                    raise ValueError("callback URLs cannot contain OAuth response parameters")
            self.clients[client.client_id] = replace(
                client, redirect_uris=tuple(client.redirect_uris)
            )
        if any(not SCOPE.fullmatch(scope) for scope in scopes):
            raise ValueError("scopes must be non-empty OAuth scope tokens")
        self.issuer = issuer
        self.upstream = upstream
        self.resource_url = resource
        self.scopes = tuple(scopes)
        self.identity = identity
        self.store = store if store is not None else MemorySessionStore()
        self.access_token_ttl = access_token_ttl
        self.prefix = f"oauth/{digest(issuer + '|' + resource)}/"
        self.base_path = issuer_url.path.rstrip("/")
        self.origin = str(issuer_url.origin())
        self.callback_url = issuer + "/callback"
        self._http: ClientSession | None = None

    def resource(self, *, required_scopes: Sequence[str] = ()) -> Authorization:
        """Return the Registry authentication policy bound to this resource and issuer."""
        if set(required_scopes) - set(self.scopes):
            raise ValueError("required scopes must be declared by the facade")
        return Authorization(
            verifier=self,
            resource=self.resource_url,
            authorization_servers=(self.issuer,),
            scopes_supported=self.scopes,
            required_scopes=tuple(required_scopes),
        )

    def metadata(self) -> dict[str, Any]:
        return {
            "issuer": self.issuer,
            "authorization_endpoint": self.issuer + "/authorize",
            "token_endpoint": self.issuer + "/token",
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code"],
            "token_endpoint_auth_methods_supported": ["none"],
            "code_challenge_methods_supported": ["S256"],
            "authorization_response_iss_parameter_supported": True,
            "scopes_supported": list(self.scopes),
        }

    def routes(self) -> list[web.RouteDef]:
        """Mount at the origin root. Paths come from the configured issuer."""
        return [
            metadata_route(
                web.get(
                    "/.well-known/oauth-authorization-server" + self.base_path,
                    self.handle_metadata,
                )
            ),
            web.get(self.base_path + "/authorize", self.handle_authorize, allow_head=False),
            web.post(self.base_path + "/authorize", self.handle_consent),
            web.get(self.base_path + "/callback", self.handle_callback, allow_head=False),
            web.post(self.base_path + "/token", self.handle_token),
        ]

    def setup(self, app: web.Application) -> web.Application:
        """Add OAuth routes and manage the upstream HTTP session with the application."""
        app.add_routes(self.routes())
        app.cleanup_ctx.append(self.cleanup_ctx)
        return app

    async def cleanup_ctx(self, app: web.Application) -> AsyncIterator[None]:
        if self._http is not None:
            raise RuntimeError("an OAuthFacade instance can run in only one application")
        async with ClientSession(
            timeout=ClientTimeout(total=15), cookie_jar=DummyCookieJar(), trust_env=False
        ) as http:
            self._http = http
            try:
                yield
            finally:
                self._http = None

    @staticmethod
    def is_public(request: web.Request) -> bool:
        """Identify facade routes for application authentication middleware.

        These handlers enforce their own OAuth checks and must be reachable
        before the caller has an MCP token.
        """
        handler = request.match_info.handler
        facade = getattr(handler, "__self__", None)
        if not isinstance(facade, OAuthFacade):
            return False
        return any(
            handler == route.handler
            and (
                request.method == route.method
                or (request.method == "HEAD" and route.method == "GET")
            )
            for route in facade.routes()
        )

    def key(self, kind: str, token: str) -> str:
        return self.prefix + kind + "/" + digest(token)

    async def put(self, kind: str, data: Mapping[str, Any], ttl: int = FLOW_TTL) -> str:
        # Remove expired rows in the default process-local store. Other backends own expiry.
        if isinstance(self.store, MemorySessionStore):
            for key in tuple(self.store.records):
                if key.startswith(self.prefix):
                    self.store.live(key)
        token = secrets.token_urlsafe(32)
        saved = await self.store.create(
            self.key(kind, token), {**data, "expires_at": time.time() + ttl}, ttl_seconds=ttl
        )
        if not saved:
            raise RuntimeError("the OAuth store refused a fresh identifier")
        return token

    async def read(self, kind: str, token: str) -> SessionRecord:
        if not OPAQUE.fullmatch(token):
            raise OAuthFailure("invalid_grant", "The code or token is invalid or expired.")
        record = await self.store.get(self.key(kind, token))
        if record is None or record.data.get("used") or record.data["expires_at"] <= time.time():
            raise OAuthFailure("invalid_grant", "The code or token is invalid or expired.")
        return record

    async def take(self, kind: str, token: str, record: SessionRecord) -> None:
        key = self.key(kind, token)
        won = await self.store.save(
            key, {"used": True}, expected_version=record.version, ttl_seconds=FLOW_TTL
        )
        if not won:
            raise OAuthFailure("invalid_grant", "The code has already been used.")
        await self.store.delete(key)

    async def verify(self, token: str) -> Principal | None:
        try:
            record = await self.read("access", token)
        except OAuthFailure:
            return None
        data = record.data
        if data.get("resource") != self.resource_url or data.get("issuer") != self.issuer:
            return None
        principal = dict(data["principal"])
        principal["scopes"] = frozenset(principal["scopes"])
        return Principal(**principal)

    async def revoke(self, token: str) -> None:
        """Revoke a locally issued access token without sending it to the provider."""
        await self.store.delete(self.key("access", token))

    def client(self, client_id: str) -> OAuthClient:
        found = self.clients.get(client_id)
        if found is None:
            raise OAuthFailure("invalid_client", "The OAuth client is not registered.")
        return found

    @staticmethod
    def unique(params: Any) -> dict[str, str]:
        if any(len(params.getall(key)) != 1 for key in params):
            raise OAuthFailure("invalid_request", "Duplicate OAuth parameters are not supported.")
        if any(not isinstance(value, str) or len(value) > 4096 for value in params.values()):
            raise OAuthFailure("invalid_request", "Invalid OAuth parameter.")
        return dict(params)

    @staticmethod
    def failure(error: OAuthFailure) -> web.Response:
        return web.json_response(
            {"error": error.error, "error_description": error.description},
            status=error.status,
            headers=PRIVATE_HEADERS,
        )

    def redirect(self, flow: Mapping[str, Any], **params: str) -> web.Response:
        target = URL(flow["redirect_uri"]).update_query(
            {**params, "state": flow.get("state", ""), "iss": self.issuer}
        )
        return web.Response(status=303, headers={**PRIVATE_HEADERS, "Location": str(target)})

    def cookie_name(self, flow: Mapping[str, Any]) -> str:
        return "mcp-oauth-" + flow["browser_id"]

    def bound_browser(self, request: web.Request, flow: Mapping[str, Any]) -> None:
        value = request.cookies.get(self.cookie_name(flow), "")
        if not value or not secrets.compare_digest(digest(value), flow["browser_hash"]):
            raise OAuthFailure("invalid_request", "The login does not belong to this browser.")

    def clear_cookie(self, response: web.Response, flow: Mapping[str, Any]) -> web.Response:
        response.del_cookie(self.cookie_name(flow), path=self.base_path or "/")
        return response

    def bind_cookie(
        self, response: web.Response, flow: Mapping[str, Any], browser: str
    ) -> web.Response:
        response.set_cookie(
            self.cookie_name(flow),
            browser,
            max_age=FLOW_TTL,
            path=self.base_path or "/",
            httponly=True,
            samesite="Lax",
            secure=URL(self.issuer).scheme == "https",
        )
        return response

    async def start_upstream(self, flow: Mapping[str, Any]) -> web.Response:
        verifier = secrets.token_urlsafe(32)
        state = await self.put("upstream", {**flow, "verifier": verifier})
        target = self.upstream.authorization_url(
            redirect_uri=self.callback_url, state=state, challenge=challenge(verifier)
        )
        return web.Response(status=303, headers={**PRIVATE_HEADERS, "Location": target})

    async def handle_metadata(self, request: web.Request) -> web.Response:
        return web.json_response(self.metadata(), headers={"Cache-Control": "public, max-age=3600"})

    async def handle_authorize(self, request: web.Request) -> web.Response:
        flow: dict[str, Any] | None = None
        try:
            params = self.unique(request.query)
            client = self.client(params.get("client_id", ""))
            redirect_uri = params.get("redirect_uri", "")
            if redirect_uri not in client.redirect_uris:
                raise OAuthFailure("invalid_request", "The callback URL is not registered.")
            flow = {**params, "redirect_uri": redirect_uri}
            if params.get("response_type") != "code":
                raise OAuthFailure(
                    "unsupported_response_type", "Only authorization codes are supported."
                )
            if params.get("resource") != self.resource_url:
                raise OAuthFailure("invalid_target", "The requested MCP resource does not match.")
            if params.get("code_challenge_method") != "S256" or not OPAQUE.fullmatch(
                params.get("code_challenge", "")
            ):
                raise OAuthFailure("invalid_request", "PKCE S256 is required.")
            scopes = params.get("scope", " ".join(self.scopes)).split()
            if set(scopes) - set(self.scopes):
                raise OAuthFailure("invalid_scope", "The requested scope is not available.")
            browser = secrets.token_urlsafe(32)
            flow.update(
                scope=" ".join(sorted(set(scopes))),
                browser_id=secrets.token_hex(12),
                browser_hash=digest(browser),
            )
            if not client.require_consent:
                return self.bind_cookie(await self.start_upstream(flow), flow, browser)
            ticket = await self.put("consent", flow)
            action = escape(self.base_path + "/authorize", quote=True)
            # Chrome applies form-action to the POST's redirect chain as well.
            form_origins = " ".join(
                sorted(
                    {
                        str(URL(self.upstream.authorize_url).origin()),
                        str(URL(redirect_uri).origin()),
                    }
                )
            )
            text = (
                '<!doctype html><html lang="en"><meta charset="utf-8">'
                '<meta name="viewport" content="width=device-width, initial-scale=1">'
                "<title>Authorize MCP access</title><body><h1>Authorize MCP access</h1>"
                f"<p><strong>{escape(client.client_name)}</strong> requests access to "
                f"{escape(self.resource_url)}.</p>"
                f"<p>Return address: {escape(redirect_uri)}</p>"
                f"<p>Permissions: {escape(flow['scope']) or 'Sign in'}</p>"
                f"<p>Continue with {escape(self.upstream.name)} only if you trust this client.</p>"
                f'<form method="post" action="{action}">'
                f'<input type="hidden" name="ticket" value="{ticket}">'
                '<button name="decision" value="allow">Continue</button> '
                '<button name="decision" value="deny">Cancel</button></form></body></html>'
            )
            response = web.Response(
                text=text,
                content_type="text/html",
                headers={
                    **PRIVATE_HEADERS,
                    # A non-CORS form POST with no-referrer gets Origin: null.
                    "Referrer-Policy": "same-origin",
                    "Content-Security-Policy": (
                        f"default-src 'none'; form-action 'self' {form_origins}; "
                        "frame-ancestors 'none'; base-uri 'none'"
                    ),
                },
            )
            return self.bind_cookie(response, flow, browser)
        except OAuthFailure as error:
            if flow is not None:
                return self.redirect(flow, error=error.error, error_description=error.description)
            return self.failure(error)

    async def handle_consent(self, request: web.Request) -> web.Response:
        try:
            if request.headers.get("Origin") != self.origin:
                raise OAuthFailure("invalid_request", "The consent origin does not match.", 403)
            if request.content_type != "application/x-www-form-urlencoded":
                raise OAuthFailure("invalid_request", "Consent requires form data.")
            params = self.unique(await request.post())
            ticket = params.get("ticket", "")
            record = await self.read("consent", ticket)
            flow = record.data
            self.bound_browser(request, flow)
            await self.take("consent", ticket, record)
            if params.get("decision") != "allow":
                return self.clear_cookie(self.redirect(flow, error="access_denied"), flow)
            return await self.start_upstream(flow)
        except OAuthFailure as error:
            return self.failure(error)

    async def handle_callback(self, request: web.Request) -> web.Response:
        flow: Mapping[str, Any] | None = None
        try:
            params = self.unique(request.query)
            state = params.get("state", "")
            record = await self.read("upstream", state)
            self.bound_browser(request, record.data)
            await self.take("upstream", state, record)
            flow = record.data
            if "error" in params or not params.get("code"):
                raise OAuthFailure("access_denied", "Sign-in was not completed.")
            if self._http is None:
                raise RuntimeError("use OAuthFacade.setup() or install its cleanup_ctx")
            tokens = await self.upstream.exchange(
                self._http,
                code=params["code"],
                redirect_uri=self.callback_url,
                verifier=flow["verifier"],
            )
            identity = await self.upstream.profile(tokens, self._http)
            if not identity.sub or not identity.provider:
                raise UpstreamAuthError("The identity provider returned an empty identity.")
            principal = (
                self.identity(identity)
                if self.identity is not None
                else Principal(
                    subject=f"{identity.provider}:{identity.sub}",
                    scopes=frozenset(self.scopes),
                    claims=identity.claims,
                )
            )
            if inspect.isawaitable(principal):
                principal = await principal
            if principal is None or not principal.subject or principal.expired:
                raise OAuthFailure("access_denied", "This account cannot access the MCP server.")
            wanted = frozenset(flow["scope"].split())
            if wanted - principal.scopes:
                raise OAuthFailure("access_denied", "This account lacks the requested permissions.")
            expiry = time.time() + self.access_token_ttl
            if principal.expires_at is not None:
                expiry = min(expiry, principal.expires_at)
            principal = replace(
                principal,
                issuer=self.issuer,
                client_id=flow["client_id"],
                scopes=wanted,
                expires_at=expiry,
            )
            value = asdict(principal)
            value["scopes"] = sorted(wanted)
            # Fail before storage if the application's mapper returns non-JSON claims.
            json.dumps(value, allow_nan=False)
            code = await self.put("code", {**flow, "principal": value})
            return self.clear_cookie(self.redirect(flow, code=code), flow)
        except UpstreamAuthError:
            error = OAuthFailure(
                "temporarily_unavailable", "The identity provider could not complete sign-in."
            )
        except OAuthFailure as refusal:
            error = refusal
        if flow is not None:
            return self.clear_cookie(
                self.redirect(flow, error=error.error, error_description=error.description), flow
            )
        return self.failure(error)

    async def handle_token(self, request: web.Request) -> web.Response:
        try:
            if request.content_type != "application/x-www-form-urlencoded":
                raise OAuthFailure("invalid_request", "The token endpoint requires form data.")
            params = self.unique(await request.post())
            client = self.client(params.get("client_id", ""))
            if params.get("grant_type") != "authorization_code":
                raise OAuthFailure(
                    "unsupported_grant_type", "Only authorization_code is supported."
                )
            if params.get("resource") != self.resource_url:
                raise OAuthFailure("invalid_target", "The requested MCP resource does not match.")
            code = params.get("code", "")
            record = await self.read("code", code)
            flow = record.data
            if flow["client_id"] != client.client_id or flow["redirect_uri"] != params.get(
                "redirect_uri"
            ):
                raise OAuthFailure(
                    "invalid_grant", "The authorization code belongs to another client."
                )
            verifier = params.get("code_verifier", "")
            if not VERIFIER.fullmatch(verifier) or not secrets.compare_digest(
                challenge(verifier), flow["code_challenge"]
            ):
                raise OAuthFailure("invalid_grant", "The PKCE verifier does not match.")
            ttl = int(flow["principal"]["expires_at"] - time.time())
            if ttl <= 0:
                raise OAuthFailure("invalid_grant", "The authenticated identity has expired.")
            await self.take("code", code, record)
            token = await self.put(
                "access",
                {
                    "principal": flow["principal"],
                    "resource": self.resource_url,
                    "issuer": self.issuer,
                },
                ttl,
            )
            return web.json_response(
                {
                    "access_token": token,
                    "token_type": "Bearer",
                    "expires_in": ttl,
                    "scope": flow["scope"],
                },
                headers=PRIVATE_HEADERS,
            )
        except OAuthFailure as error:
            return self.failure(error)
