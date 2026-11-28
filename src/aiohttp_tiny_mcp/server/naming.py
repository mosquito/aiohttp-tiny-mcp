"""Registration-time checks on a tool name and a resource URI.

A name a host refuses, or a URI no request can reach, is found here rather than by the
host. Both checks run when the declaration is registered, before a protocol revision is
selected, because neither rule varies by revision.
"""

from __future__ import annotations

import logging
import re
from functools import lru_cache

log = logging.getLogger("aiohttp_tiny_mcp")

TOOL_NAME: re.Pattern = re.compile(r"[A-Za-z0-9._-]{1,128}")
"""The character set and length a tool name has (SEP-986, from revision 2025-11-25)."""

VARIABLE_NAME: re.Pattern = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
"""A template variable becomes a field of a Pydantic model, so it is a Python name."""

RFC6570_OPERATORS = "#./;?&=,!@|"
"""Unsupported expression operators in RFC 6570."""

MAX_URI_LENGTH = 8192
"""The longest URI a template may have. The same limit a host applies."""

MAX_VARIABLES = 256
"""The most variables one template may have. The same limit a host applies."""


def checked_tool_name(name: str) -> str:
    """Return `name`, or refuse a name a host is allowed to reject.

    The rule is the specification's own: one to 128 characters, each a letter, a digit,
    `.`, `_` or `-`. A space, a comma and any other character are refused here, where
    the mistake is cheap to correct. A leading or trailing `-` or `.` is only
    discouraged, so it logs and registers.
    """
    if not TOOL_NAME.fullmatch(name):
        raise ValueError(
            f"invalid tool name: {name!r}. Use 1 to 128 characters, each a letter, a "
            f"digit, '.', '_' or '-'."
        )
    if name[0] in "-." or name[-1] in "-.":
        log.warning(
            "the tool name %r starts or ends with '-' or '.', which a host may reject", name
        )
    return name


@lru_cache
def template_variables(uri: str) -> tuple[str, ...]:
    """Validate the URI and return the variable names in template order.

    A simple variable matches one segment. A reserved variable can span segments.
    More than one reserved variable makes the segment boundaries ambiguous.
    """
    if not uri:
        raise ValueError("a resource URI must not be empty")
    if len(uri) > MAX_URI_LENGTH:
        raise ValueError(f"a resource URI must be at most {MAX_URI_LENGTH} characters")
    names: list[str] = []
    index, after_previous = 0, -1
    reserved = False
    while index < len(uri):
        if uri[index] == "}":
            raise ValueError(f"{uri}: the '}}' at {index} closes nothing")
        if uri[index] != "{":
            index += 1
            continue
        end = uri.find("}", index)
        if end < 0:
            raise ValueError(f"{uri}: the '{{' at {index} is never closed")
        name = uri[index + 1 : end]
        if name and name[0] in RFC6570_OPERATORS:
            raise ValueError(
                f"{uri}: {{{name}}} uses the RFC 6570 operator {name[0]!r}, which this "
                f"package does not implement. Use {{name}} or {{+name}}."
            )
        is_reserved = name.startswith("+")
        name = name.removeprefix("+")
        if not VARIABLE_NAME.fullmatch(name):
            raise ValueError(
                f"{uri}: {name!r} is not a variable name. Start with a letter or '_', "
                f"then use letters, digits or '_'."
            )
        if name in names:
            raise ValueError(f"{uri}: the variable {name!r} appears more than once")
        if index == after_previous:
            raise ValueError(
                f"{uri}: the variables {names[-1]!r} and {name!r} have no literal text "
                f"between them, so no address can be divided between them"
            )
        if is_reserved:
            if reserved:
                raise ValueError(f"{uri}: a template can have only one reserved variable")
            reserved = True
        names.append(name)
        if len(names) > MAX_VARIABLES:
            raise ValueError(f"{uri}: a template may have at most {MAX_VARIABLES} variables")
        index = after_previous = end + 1
    return tuple(names)


__all__ = [
    "MAX_URI_LENGTH",
    "MAX_VARIABLES",
    "RFC6570_OPERATORS",
    "TOOL_NAME",
    "VARIABLE_NAME",
    "checked_tool_name",
    "template_variables",
]
