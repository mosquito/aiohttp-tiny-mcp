"""OAuth sign-in for registered MCP clients, with server-side identity providers."""

from .github import GitHub
from .provider import Identity, OAuthProvider, UpstreamAuthError
from .server import OAuthClient, OAuthServer
from .tokens import AbstractCipher, EncryptedTokens, KECCAKCipher, OpaqueTokens

__all__ = [
    "AbstractCipher",
    "EncryptedTokens",
    "GitHub",
    "Identity",
    "KECCAKCipher",
    "OAuthProvider",
    "OAuthClient",
    "OAuthServer",
    "OpaqueTokens",
    "UpstreamAuthError",
]
