"""Aiohttp access logging without token query values."""

from __future__ import annotations

import logging
from urllib.parse import unquote_plus

from aiohttp.log import access_logger


def mask_token(url: str) -> str:
    """Mask each token query value while preserving the rest of the URL."""
    path, separator, remaining = url.partition("?")
    if not separator:
        return url
    query, fragment_separator, fragment = remaining.partition("#")
    fields = []
    for field in query.split("&"):
        name, equals, value = field.partition("=")
        if equals and unquote_plus(name).casefold() == "token":
            field = f"{name}=[REDACTED]"
        fields.append(field)
    return path + separator + "&".join(fields) + fragment_separator + fragment


def mask_request_line(value: str) -> str:
    """Mask the request target without changing the method or protocol."""
    method, separator, target = value.partition(" ")
    url, protocol_separator, protocol = target.rpartition(" ")
    return method + separator + mask_token(url) + protocol_separator + protocol


class TokenQueryFilter(logging.Filter):
    """Remove token query values from standard aiohttp access log records."""

    def filter(self, record: logging.LogRecord) -> bool:
        replacements: dict[str, str] = {}
        request_line = getattr(record, "first_request_line", None)
        if isinstance(request_line, str):
            masked = mask_request_line(request_line)
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
                masked = mask_token(value)
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


TOKEN_QUERY_FILTER = TokenQueryFilter()


def install_token_filter() -> None:
    """Enable token masking once without replacing aiohttp logging configuration."""
    access_logger.addFilter(TOKEN_QUERY_FILTER)
