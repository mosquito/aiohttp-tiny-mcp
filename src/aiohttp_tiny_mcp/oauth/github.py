"""GitHub.com OAuth App configuration and stable user identity lookup."""

from __future__ import annotations

from collections.abc import Sequence

from aiohttp import ClientSession

from .upstream import Identity, OAuth2, UpstreamAuthError, UpstreamTokens, provider_json


async def github_identity(tokens: UpstreamTokens, http: ClientSession) -> Identity:
    data = await provider_json(
        http,
        "GET",
        "https://api.github.com/user",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {tokens.access_token}",
            "User-Agent": "aiohttp-tiny-mcp",
        },
    )
    if type(data.get("id")) is not int or data["id"] <= 0 or not isinstance(data.get("login"), str):
        raise UpstreamAuthError("GitHub returned no valid user identity.")
    return Identity(
        sub=str(data["id"]),
        provider="github",
        claims={key: data.get(key) for key in ("login", "name", "email")},
    )


def GitHub(client_id: str, client_secret: str, *, scopes: Sequence[str] = ("read:user",)) -> OAuth2:
    """GitHub.com OAuth App sign-in. Register the facade's callback URL in GitHub."""
    return OAuth2(
        name="GitHub",
        authorize_url="https://github.com/login/oauth/authorize",
        token_url="https://github.com/login/oauth/access_token",
        client_id=client_id,
        client_secret=client_secret,
        scopes=tuple(scopes),
        profile=github_identity,
    )


__all__ = ["GitHub"]
