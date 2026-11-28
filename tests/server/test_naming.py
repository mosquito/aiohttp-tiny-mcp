"""The registration-time rules for a tool name and a resource URI."""

from __future__ import annotations

import logging

import pytest

from aiohttp_tiny_mcp.server.naming import (
    MAX_URI_LENGTH,
    MAX_VARIABLES,
    checked_tool_name,
    template_variables,
)


@pytest.mark.parametrize(
    "name",
    [
        "add",
        "mcp_resources_list",
        "a.b-c",
        "A",
        "0",
        "a" * 128,
    ],
)
def test_a_name_within_the_specification_passes(name):
    assert checked_tool_name(name) == name


@pytest.mark.parametrize(
    "name",
    [
        "",
        "a" * 129,
        "my tool",
        "a,b",
        "search/items",
        "tool:read",
        "Stra\u00dfe",
        "emoji\U0001f600",
    ],
)
def test_a_name_outside_the_character_set_is_refused(name):
    with pytest.raises(ValueError, match="invalid tool name"):
        checked_tool_name(name)


@pytest.mark.parametrize("name", ["-lead", "trail-", ".lead", "trail."])
def test_a_leading_or_trailing_dash_or_dot_only_warns(name, caplog):
    """The specification discourages these rather than refusing them."""
    with caplog.at_level(logging.WARNING, logger="aiohttp_tiny_mcp"):
        assert checked_tool_name(name) == name
    assert "may reject" in caplog.text


@pytest.mark.parametrize(
    ("uri", "variables"),
    [
        ("config://app", []),
        ("mcp-extensions://example/manifest.json", []),
        ("db://users/{id}", ["id"]),
        ("files://{path}", ["path"]),
        ("chat://channels/{channel}/messages/{after}", ["channel", "after"]),
        ("skill://{name}/SKILL.md", ["name"]),
        ("mcp-extensions://example/echo?params={params}", ["params"]),
        ("file://Stra\u00dfe/{name}", ["name"]),
        ("x://{a}-{b}", ["a", "b"]),
    ],
)
def test_a_supported_uri_returns_its_variables(uri, variables):
    """These are the forms the package serves, so each must keep parsing."""
    assert template_variables(uri) == tuple(variables)


@pytest.mark.parametrize(
    ("uri", "fault"),
    [
        ("", "must not be empty"),
        ("notebook://entries/{id", "is never closed"),
        ("x://a}b", "closes nothing"),
        ("x://{}", "is not a variable name"),
        ("x://{1id}", "is not a variable name"),
        ("x://{+path}", "RFC 6570 operator"),
        ("x://{#frag}", "RFC 6570 operator"),
        ("x://{?query}", "RFC 6570 operator"),
        ("x://a/{id}/b/{id}", "appears more than once"),
        ("x://{id}{path}", "no literal text between them"),
    ],
)
def test_a_malformed_uri_names_its_fault(uri, fault):
    with pytest.raises(ValueError, match=fault):
        template_variables(uri)


def test_the_length_and_variable_limits_are_enforced():
    """The limits a host applies, so a template refused there is refused here."""
    with pytest.raises(ValueError, match="at most 8192 characters"):
        template_variables("x://" + "a" * MAX_URI_LENGTH)
    too_many = "x://" + "".join(f"{{v{index}}}/" for index in range(MAX_VARIABLES + 1))
    with pytest.raises(ValueError, match=f"at most {MAX_VARIABLES} variables"):
        template_variables(too_many)
