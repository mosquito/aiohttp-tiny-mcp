"""OAuth identity providers. Provider access tokens stay on the server."""

from __future__ import annotations

import asyncio
import ipaddress
import json
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from aiohttp import ClientError, ClientSession
from yarl import URL


class UpstreamAuthError(Exception):
    """The identity provider refused the request or returned an invalid response."""


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
class Identity:
    """A stable provider identity. Claims must not contain credentials."""

    sub: str
    provider: str
    claims: Mapping[str, Any] = field(default_factory=dict)


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
class OAuthProvider:
    """Configure an OAuth authorization-code provider and its identity lookup."""

    name: str
    authorize_url: str
    token_url: str
    client_id: str
    scopes: Sequence[str]
    profile: Callable[[dict[str, Any], ClientSession], Awaitable[Identity]]
    client_secret: str = field(default="", repr=False)
    extra_authorize_params: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        checked_url(self.authorize_url, query=True)
        checked_url(self.token_url)
        if not self.client_id:
            raise ValueError("the provider client ID is required")

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
    ) -> dict[str, Any]:
        """Exchange the code and return the complete provider response dictionary."""
        form = {
            "grant_type": "authorization_code",
            "client_id": self.client_id,
            "code": code,
            "redirect_uri": redirect_uri,
            "code_verifier": verifier,
        }
        if self.client_secret:
            form["client_secret"] = self.client_secret
        data = await provider_json(
            http,
            "POST",
            self.token_url,
            headers={"Accept": "application/json"},
            data=form,
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
        if "scope" in data and not isinstance(data["scope"], str):
            raise UpstreamAuthError("The identity provider returned an invalid scope.")
        return data
