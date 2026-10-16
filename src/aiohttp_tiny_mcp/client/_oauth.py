"""OAuth validation and HTTP challenge parsing shared by the client helpers."""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Iterable
from urllib.request import parse_http_list

from yarl import URL


class OAuthError(Exception):
    """An OAuth failure. Messages exclude response bodies and credentials."""


class AuthorizationRequired(OAuthError):
    """The application must obtain authorization before it can continue."""

    def __init__(self, scopes: Iterable[str] = ()) -> None:
        super().__init__("Interactive authorization is required.")
        self.scopes = tuple(sorted(set(scopes)))


class RegistrationRequired(OAuthError):
    """The application must supply client registration information."""


def checked_url(value: object, *, allow_http: bool = False) -> URL:
    """Require HTTPS, or explicitly allowed HTTP on a loopback host."""
    try:
        if not isinstance(value, str) or not value or any(ord(c) < 33 for c in value):
            raise ValueError
        url = URL(value)
        host = url.host
        port = url.port
        if not host or not port or url.user is not None or url.fragment:
            raise ValueError
        loopback = host == "localhost"
        try:
            loopback = loopback or ipaddress.ip_address(host).is_loopback
        except ValueError:
            pass
        if url.scheme != "https" and not (allow_http and loopback and url.scheme == "http"):
            raise ValueError
    except (ValueError, TypeError):
        raise OAuthError("OAuth URLs must use HTTPS without credentials or fragments.") from None
    return url


def scopes(value: object) -> tuple[str, ...]:
    if not isinstance(value, str):
        raise OAuthError("Invalid OAuth scope value.")
    result = tuple(value.split(" ")) if value else ()
    if any(not re.fullmatch(r"[\x21\x23-\x5b\x5d-\x7e]+", item) for item in result):
        raise OAuthError("Invalid OAuth scope value.")
    return result


def strings(value: object) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise OAuthError("Invalid OAuth metadata list.")
    return value


_PARAM = re.compile(r'([!#$%&\'*+.^_`|~\w-]+)\s*=\s*(?:"((?:[^"\\]|\\.)*)"|([^\s,]+))\Z')


def bearer_challenge(values: Iterable[str]) -> dict[str, str]:
    """Parse Bearer parameters without mixing parameters from other schemes."""
    found: dict[str, str] | None = None
    for value in values:
        if len(value) > 16384:
            raise OAuthError("Authentication challenge is too large.")
        current: dict[str, str] | None = None
        for part in parse_http_list(value):
            part = part.strip()
            match = _PARAM.fullmatch(part)
            if match is None:
                scheme, _, part = part.partition(" ")
                current = {} if scheme.lower() == "bearer" else None
                if current is not None:
                    if found is not None:
                        raise OAuthError("Multiple Bearer challenges are ambiguous.")
                    found = current
                match = _PARAM.fullmatch(part.strip())
                if current is not None and part and match is None:
                    raise OAuthError("Invalid Bearer challenge.")
            if current is not None and match:
                key = match[1].lower()
                if key in current:
                    raise OAuthError("Duplicate Bearer challenge parameter.")
                current[key] = (
                    re.sub(r"\\(.)", r"\1", match[2]) if match[2] is not None else match[3]
                )
    return found or {}
