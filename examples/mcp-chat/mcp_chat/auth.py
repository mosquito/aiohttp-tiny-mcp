"""Demo identities and signed organization-scoped JWT credentials."""

from __future__ import annotations

import re
import time
from collections.abc import Mapping
from secrets import compare_digest
from typing import Any

import jwt
from aiohttp import web
from aiohttp_tiny_mcp import Authentication, Principal, Unauthorized, principal_from_claims
from aiohttp_tiny_mcp.auth.jwt import HMACJWTVerifier

from .store import Store

ISSUER = "mcp-chat"
AUDIENCE = "mcp-chat"
MAX_TOKEN_TTL = 86400
WEB_AUDIENCE = "mcp-chat-web"
WEB_COOKIE = "mcp_chat_web"
WEB_TTL = 3600
NO_STORE = {"Cache-Control": "no-store"}


def validated_org(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9.-]{0,252}", value):
        raise ValueError("org must be a nonempty domain-style identifier.")
    return value.lower()


def validated_user(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.@-]{0,63}", value):
        raise ValueError("user must contain 1–64 letters, digits, or . _ @ -.")
    return value


def request_context(request: web.Request) -> tuple[str, str | None]:
    if len(request.query.getall("org", [])) != 1:
        raise web.HTTPBadRequest(text="Supply exactly one org query parameter.")
    if len(request.query.getall("user", [])) > 1:
        raise web.HTTPBadRequest(text="Supply at most one user query parameter.")
    try:
        org = validated_org(request.query["org"])
        user = validated_user(request.query["user"]) if "user" in request.query else None
    except ValueError as error:
        raise web.HTTPBadRequest(text=str(error)) from None
    return org, user


def credentials(request: web.Request) -> str | None:
    headers = request.headers.getall("Authorization", [])
    tokens = request.query.getall("token", [])
    if len(headers) > 1 or len(tokens) > 1 or (headers and tokens):
        raise web.HTTPBadRequest(text="Supply one credential, as Bearer or token, not both.")
    if headers:
        match = re.fullmatch(r"(?i:Bearer) ([^\s,]+)", headers[0])
        if match is None:
            raise Unauthorized("invalid_token", "Supply a valid Bearer token.")
        return match[1]
    if tokens:
        if not tokens[0] or re.search(r"[\s,]", tokens[0]):
            raise Unauthorized("invalid_token", "Supply a valid token.")
        return tokens[0]
    return None


def chat_principal(claims: Mapping[str, Any]) -> Principal:
    validated = principal_from_claims(claims)
    user = validated_user(claims.get("sub"))
    org = validated_org(claims.get("org"))
    return Principal(
        subject=user,
        issuer=validated.issuer,
        expires_at=validated.expires_at,
        namespace=org,
        claims=claims,
    )


class DemoIdentity(Authentication):
    """Trust URL identities only when the application has no configured JWT key."""

    def __init__(self, store: Store) -> None:
        super().__init__()
        self.store = store

    async def authenticate(self, request: web.Request) -> Principal:
        org, user = request_context(request)
        if credentials(request) is not None:
            raise Unauthorized("invalid_token", "JWT authentication is not configured.")
        await self.store.ensure_organization(org)
        return Principal(subject=user or "human", namespace=org)

    def challenge(self, refusal: Unauthorized) -> str:
        return 'Bearer realm="mcp-chat"'


class JWTIdentity(Authentication):
    """Verify HS256 credentials before selecting the organization and user."""

    def __init__(self, store: Store, secret: str) -> None:
        super().__init__()
        self.store = store
        self.verifier = HMACJWTVerifier(
            secret, issuer=ISSUER, audience=AUDIENCE, principal=chat_principal
        )

    async def authenticate(self, request: web.Request) -> Principal:
        token = credentials(request)
        principal = await self.verifier.verify(token) if token else None
        if principal is None or principal.expired:
            raise Unauthorized("invalid_token", "A valid, unexpired JWT token is required.")
        assert principal.namespace is not None
        await self.store.ensure_organization(principal.namespace)
        return principal

    def challenge(self, refusal: Unauthorized) -> str:
        return f'Bearer realm="mcp-chat", error="{refusal.error}"'


def issue_token(secret: str, org: str, user: str, ttl_seconds: int = 3600) -> str:
    """Issue an HS256 token for one organization and user, valid for at most one day."""
    verifier = HMACJWTVerifier(secret)
    assert isinstance(verifier.key, bytes)
    if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int):
        raise ValueError("Token lifetime must be an integer number of seconds.")
    if not 1 <= ttl_seconds <= MAX_TOKEN_TTL:
        raise ValueError(f"Token lifetime must be between 1 and {MAX_TOKEN_TTL} seconds.")
    now = int(time.time())
    return jwt.encode(
        {
            "iss": ISSUER,
            "aud": AUDIENCE,
            "sub": validated_user(user),
            "org": validated_org(org),
            "iat": now,
            "exp": now + ttl_seconds,
        },
        verifier.key,
        algorithm="HS256",
    )


def web_principal(claims: Mapping[str, Any]) -> Principal:
    principal = principal_from_claims(claims)
    if principal.subject != "web-operator":
        raise ValueError("Invalid web session subject.")
    return principal


async def require_admin(request: web.Request, secret: str | None) -> None:
    if secret is None:
        return
    verifier = HMACJWTVerifier(
        secret, issuer=ISSUER, audience=WEB_AUDIENCE, principal=web_principal
    )
    cookie = request.cookies.get(WEB_COOKIE)
    principal = await verifier.verify(cookie) if cookie else None
    if principal is None or principal.expired:
        raise web.HTTPUnauthorized(text="Sign in with the server secret.", headers=NO_STORE)


async def auth_arguments(request: web.Request, fields: frozenset[str]) -> dict[str, Any]:
    origin = request.headers.get("Origin")
    if origin is not None and origin != str(request.url.origin()):
        raise web.HTTPForbidden(text="Origin is not allowed.", headers=NO_STORE)
    if request.headers.get("Sec-Fetch-Site") == "cross-site":
        raise web.HTTPForbidden(text="Cross-site requests are not allowed.", headers=NO_STORE)
    if request.content_type != "application/json":
        raise web.HTTPUnsupportedMediaType(text="Send application/json.", headers=NO_STORE)
    try:
        body = await request.json()
    except (ValueError, UnicodeDecodeError):
        raise web.HTTPBadRequest(text="Invalid JSON body.", headers=NO_STORE) from None
    if not isinstance(body, dict) or not body.keys() <= fields:
        raise web.HTTPBadRequest(text="Invalid authentication arguments.", headers=NO_STORE)
    return body


def scoped_token(secret: str, args: Mapping[str, Any], ttl: int) -> str:
    try:
        return issue_token(
            secret, validated_org(args.get("org")), validated_user(args.get("user")), ttl
        )
    except ValueError as error:
        raise web.HTTPBadRequest(text=str(error), headers=NO_STORE) from None


def setup_auth(app: web.Application, secret: str | None) -> None:
    """Mount web login and token issuance only when JWT authentication is configured."""
    if secret is None:
        return
    verifier = HMACJWTVerifier(
        secret, issuer=ISSUER, audience=WEB_AUDIENCE, principal=web_principal
    )
    assert isinstance(verifier.key, bytes)
    key = verifier.key

    async def login(request: web.Request) -> web.Response:
        args = await auth_arguments(request, frozenset({"secret", "org", "user"}))
        supplied = args.get("secret")
        if not isinstance(supplied, str) or not compare_digest(supplied.encode("utf-8"), key):
            raise web.HTTPUnauthorized(text="Invalid sign-in secret.", headers=NO_STORE)
        token = scoped_token(secret, args, WEB_TTL)
        now = int(time.time())
        cookie = jwt.encode(
            {
                "iss": ISSUER,
                "aud": WEB_AUDIENCE,
                "sub": "web-operator",
                "iat": now,
                "exp": now + WEB_TTL,
            },
            key,
            algorithm="HS256",
        )
        response = web.json_response({"token": token, "expires_in": WEB_TTL}, headers=NO_STORE)
        response.set_cookie(
            WEB_COOKIE,
            cookie,
            max_age=WEB_TTL,
            path="/admin",
            secure=request.secure,
            httponly=True,
            samesite="Strict",
        )
        return response

    async def token(request: web.Request) -> web.Response:
        args = await auth_arguments(request, frozenset({"org", "user", "ttl_seconds"}))
        await require_admin(request, secret)
        ttl = args.get("ttl_seconds", WEB_TTL)
        return web.json_response(
            {"token": scoped_token(secret, args, ttl), "expires_in": ttl}, headers=NO_STORE
        )

    app.router.add_post("/admin/auth", login)
    app.router.add_post("/admin/token", token)
