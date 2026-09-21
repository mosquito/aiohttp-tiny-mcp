"""OAuth sign-in for registered MCP clients, with server-side identity providers."""

from .facade import OAuthClient, OAuthFacade
from .github import GitHub
from .upstream import Identity, OAuth2, UpstreamAuthError, UpstreamTokens

__all__ = [
    "GitHub",
    "Identity",
    "OAuth2",
    "OAuthClient",
    "OAuthFacade",
    "UpstreamAuthError",
    "UpstreamTokens",
]
