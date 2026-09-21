"""OAuth identity providers. Provider access tokens stay on the server."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from aiohttp import ClientError, ClientSession
from yarl import URL


class UpstreamAuthError(Exception):
    """The identity provider refused the request or returned an invalid response."""


@dataclass(frozen=True)
class Identity:
    """A stable provider identity. Claims must not contain credentials."""

    sub: str
    provider: str
    claims: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class UpstreamTokens:
    """Provider credentials, separate from the MCP principal and access token."""

    access_token: str = field(repr=False)
    refresh_token: str | None = field(default=None, repr=False)
    expires_at: float | None = None
    scope: str = ""


async def provider_json(http: ClientSession, method: str, url: str, **kwargs: Any) -> dict:
    """Read a bounded JSON response without following credential-bearing redirects."""
    try:
        async with http.request(method, url, allow_redirects=False, **kwargs) as response:
            if response.status != 200:
                raise UpstreamAuthError("The identity provider refused the request.")
            body = bytearray()
            async for chunk in response.content.iter_chunked(8192):
                body.extend(chunk)
                if len(body) > 65536:
                    raise UpstreamAuthError("The identity provider response is too large.")
            data = json.loads(body)
    except (ClientError, asyncio.TimeoutError, ValueError) as error:
        raise UpstreamAuthError("The identity provider response could not be read.") from error
    if not isinstance(data, dict) or "error" in data:
        raise UpstreamAuthError("The identity provider returned an error.")
    return data


@dataclass(frozen=True)
class OAuth2:
    """Configure an OAuth authorization-code provider and its identity lookup."""

    name: str
    authorize_url: str
    token_url: str
    client_id: str
    client_secret: str = field(repr=False)
    scopes: Sequence[str]
    profile: Callable[[UpstreamTokens, ClientSession], Awaitable[Identity]]
    extra_authorize_params: Mapping[str, str] = field(default_factory=dict)

    def authorization_url(self, *, redirect_uri: str, state: str, challenge: str) -> str:
        params = {
            **self.extra_authorize_params,
            "response_type": "code",
            "client_id": self.client_id,
            "redirect_uri": redirect_uri,
            "scope": " ".join(self.scopes),
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        return str(URL(self.authorize_url).update_query(params))

    async def exchange(
        self, http: ClientSession, *, code: str, redirect_uri: str, verifier: str
    ) -> UpstreamTokens:
        data = await provider_json(
            http,
            "POST",
            self.token_url,
            headers={"Accept": "application/json"},
            data={
                "grant_type": "authorization_code",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "code": code,
                "redirect_uri": redirect_uri,
                "code_verifier": verifier,
            },
        )
        token = data.get("access_token")
        if not isinstance(token, str) or not token or len(token) > 8192:
            raise UpstreamAuthError("The identity provider returned no valid access token.")
        if str(data.get("token_type", "")).lower() != "bearer":
            raise UpstreamAuthError("The identity provider returned an unsupported token type.")
        expiry = data.get("expires_in")
        if expiry is not None and (type(expiry) is not int or expiry <= 0):
            raise UpstreamAuthError("The identity provider returned an invalid token lifetime.")
        refresh = data.get("refresh_token")
        if refresh is not None and not isinstance(refresh, str):
            raise UpstreamAuthError("The identity provider returned an invalid refresh token.")
        return UpstreamTokens(
            access_token=token,
            refresh_token=refresh,
            expires_at=time.time() + expiry if expiry is not None else None,
            scope=str(data.get("scope", "")),
        )
