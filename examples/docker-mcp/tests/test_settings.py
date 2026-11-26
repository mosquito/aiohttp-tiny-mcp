"""Where settings come from: the command line, the environment and a file."""

from __future__ import annotations

import pytest

from docker_mcp.__main__ import Parser

CONFIG = """
[DEFAULT]
network = sandbox
unprivileged = true
allowed_origin = ["https://docker.example.com"]

[jwt]
psk = a-pre-shared-key-of-at-least-32-bytes
audience = https://docker.example.com/mcp
issuer = https://docker.example.com
"""


def test_the_command_line_lists_the_origins():
    parser = Parser()
    parser.parse_args(["--allowed-origin", "https://one.example.com", "https://two.example.com"])
    assert parser.allowed_origin == frozenset(
        {"https://one.example.com", "https://two.example.com"}
    )


def test_the_environment_holds_the_origins_as_a_list(monkeypatch):
    # An environment variable holds a Python literal, as a config file does.
    monkeypatch.setenv(
        "DOCKER_MCP_ALLOWED_ORIGIN", '["https://one.example.com", "https://two.example"]'
    )
    parser = Parser(auto_env_var_prefix="DOCKER_MCP_")
    parser.parse_args([])
    assert parser.allowed_origin == frozenset({"https://one.example.com", "https://two.example"})


def test_no_origin_leaves_the_set_empty():
    parser = Parser()
    parser.parse_args([])
    assert not parser.allowed_origin


def test_a_config_file_holds_every_setting(tmp_path):
    path = tmp_path / "docker-mcp.ini"
    path.write_text(CONFIG, encoding="utf-8")
    parser = Parser(config_files=[str(path)])
    parser.parse_args([])
    assert parser.network == "sandbox"
    assert parser.unprivileged is True
    assert parser.allowed_origin == frozenset({"https://docker.example.com"})
    assert str(parser.jwt.psk) == "a-pre-shared-key-of-at-least-32-bytes"
    assert parser.jwt.audience == "https://docker.example.com/mcp"
    assert parser.jwt.issuer == "https://docker.example.com"


def test_the_generated_config_names_every_setting(tmp_path):
    path = tmp_path / "generated.ini"
    with pytest.raises(SystemExit) as stopped:
        Parser().parse_args(["--generate-config", str(path)])
    assert stopped.value.code == 0
    written = path.read_text(encoding="utf-8")
    assert "[DEFAULT]" in written and "[jwt]" in written
    for key in ("state", "network", "unprivileged", "allowed_origin", "psk", "audience"):
        assert f"# {key} = " in written

    # The generated file reloads, and every setting keeps its default.
    parser = Parser(config_files=[str(path)])
    parser.parse_args([])
    assert parser.network == "" and parser.unprivileged is False
    assert not parser.allowed_origin
