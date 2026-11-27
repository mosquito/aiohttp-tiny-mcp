"""Aiohttp access logging without credential query values.

A policy that reads a credential from a URL puts it in the access log of every
proxy and server on the way. This filter masks common credential names and
the names declared by the registry's policies.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import ClassVar
from urllib.parse import unquote_plus

from aiohttp.log import access_logger


class TokenQueryFilter(logging.Filter):
    """Remove credential query values from standard aiohttp access log records."""

    DEFAULT_NAMES: ClassVar[frozenset[str]] = frozenset({"token", "access_token", "secret", "jwt"})
    """Mask these names even when a proxy or middleware reads the credentials."""

    def __init__(self, names: Iterable[str] = ()) -> None:
        super().__init__()
        self.names = frozenset(names) | self.DEFAULT_NAMES

    def filter(self, record: logging.LogRecord) -> bool:
        replacements: dict[str, str] = {}
        request_line = getattr(record, "first_request_line", None)
        if isinstance(request_line, str):
            masked = mask_request_line(request_line, self.names)
            if masked != request_line:
                replacements[request_line] = masked
                record.first_request_line = masked
        for key in ("request_header", "response_header"):
            headers = getattr(record, key, None)
            if not isinstance(headers, dict):
                continue
            redacted = dict(headers)
            for name, value in headers.items():
                if not isinstance(value, str):
                    continue
                masked = mask_token(value, self.names)
                if masked != value:
                    replacements[value] = masked
                    redacted[name] = masked
            setattr(record, key, redacted)
        if replacements:
            message = record.getMessage()
            for value in sorted(replacements, key=len, reverse=True):
                message = message.replace(value, replacements[value])
            record.msg = message
            record.args = ()
            if hasattr(record, "message"):
                record.message = message
        return True


def mask_token(url: str, names: Iterable[str] = TokenQueryFilter.DEFAULT_NAMES) -> str:
    """Mask each credential query value while preserving the rest of the URL."""
    wanted = frozenset(names)
    path, separator, remaining = url.partition("?")
    if not separator:
        return url
    query, fragment_separator, fragment = remaining.partition("#")
    fields = []
    for field in query.split("&"):
        name, equals, value = field.partition("=")
        if equals and unquote_plus(name).casefold() in wanted:
            field = f"{name}=[REDACTED]"
        fields.append(field)
    return path + separator + "&".join(fields) + fragment_separator + fragment


def mask_request_line(value: str, names: Iterable[str] = TokenQueryFilter.DEFAULT_NAMES) -> str:
    """Mask the request target without changing the method or protocol."""
    method, separator, target = value.partition(" ")
    url, protocol_separator, protocol = target.rpartition(" ")
    return method + separator + mask_token(url, names) + protocol_separator + protocol


def install_token_filter(*names: str) -> None:
    """Mask these argument names, and the ones already masked, in the access log.

    Called again with other names, it replaces its own filter with one that covers
    every name asked for so far. Nothing else in the aiohttp logging configuration
    changes, and a filter installed by someone else is left alone.
    """
    wanted = {name.casefold() for name in names}
    for installed in list(access_logger.filters):
        if isinstance(installed, TokenQueryFilter):
            wanted |= installed.names
            access_logger.removeFilter(installed)
    access_logger.addFilter(TokenQueryFilter(wanted))
