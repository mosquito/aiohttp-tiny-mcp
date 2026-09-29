from __future__ import annotations

import pytest
from jsonschema import ValidationError, validate
from pydantic import BaseModel, Field

from aiohttp_tiny_mcp.protocol.schema import (
    header_params,
    inline_refs,
    json_schema,
    simplify_legacy_schema,
)


class Inner(BaseModel):
    x: int


class Outer(BaseModel):
    inner: Inner
    name: str = Field(description="a name", json_schema_extra={"x-mcp-header": "Name"})


class Routing(BaseModel):
    region: str = Field(json_schema_extra={"x-mcp-header": "Region"})


class Routed(BaseModel):
    routing: Routing


class Wrapper(BaseModel):
    inner: Inner | None = None


def test_json_schema_keeps_refs_for_full_fidelity():
    schema = json_schema(Outer)
    assert "$defs" in schema
    assert schema["properties"]["inner"]["$ref"] == "#/$defs/Inner"


def test_inline_refs_resolves_defs():
    schema = inline_refs(json_schema(Outer))
    assert "$defs" not in schema
    assert schema["properties"]["inner"]["type"] == "object"
    assert schema["properties"]["inner"]["properties"]["x"]["type"] == "integer"


def test_degrade_policy_inlines_refs():
    schema = simplify_legacy_schema(json_schema(Outer))
    assert schema is not None
    assert "$ref" not in schema["properties"]["inner"]
    assert schema["properties"]["inner"]["properties"]["x"]["type"] == "integer"


def test_header_params_extracted():
    schema = json_schema(Outer)
    assert header_params(schema) == ((("name",), "Name"),)


def test_header_params_include_statically_reachable_nested_properties():
    schema = {
        "type": "object",
        "properties": {
            "routing": {
                "type": "object",
                "properties": {
                    "region": {"type": "string", "x-mcp-header": "Region"},
                },
            }
        },
    }
    assert header_params(schema) == ((("routing", "region"), "Region"),)


def test_header_params_follow_local_schema_references():
    assert header_params(json_schema(Routed)) == ((("routing", "region"), "Region"),)


def test_header_params_reject_invalid_or_duplicate_declarations():
    with pytest.raises(ValueError):
        header_params(
            {
                "type": "object",
                "properties": {"value": {"type": "object", "x-mcp-header": "Value"}},
            }
        )
    with pytest.raises(ValueError):
        header_params(
            {
                "type": "object",
                "properties": {
                    "a": {"type": "string", "x-mcp-header": "Region"},
                    "b": {"type": "string", "x-mcp-header": "region"},
                },
            }
        )


def test_degrade_policy_drops_multi_branch_oneof():
    schema = {
        "type": "object",
        "properties": {
            "x": {"oneOf": [{"type": "string"}, {"type": "integer"}]},
        },
    }
    assert simplify_legacy_schema(schema) is None


def test_degrade_policy_collapses_single_anyof():
    schema = {
        "type": "object",
        "properties": {
            "x": {"anyOf": [{"type": "string"}]},
        },
    }
    out = simplify_legacy_schema(schema)
    assert out is not None
    assert out["properties"]["x"] == {"type": "string"}


def test_degrade_policy_strips_conditionals():
    schema = {"type": "object", "properties": {}, "if": {}, "then": {}, "else": {}}
    out = simplify_legacy_schema(schema)
    assert out is not None
    assert "if" not in out and "then" not in out and "else" not in out


def test_degrade_policy_rejects_non_object_root():
    assert simplify_legacy_schema({"type": "string"}) is None


class Optional(BaseModel):
    key: str
    handle: str | None = None


def test_optional_field_does_not_hide_the_declaration():
    """Nullable fields must not hide legacy tools as unsupported unions."""
    out = simplify_legacy_schema(json_schema(Optional))
    assert out is not None
    assert out["properties"]["handle"]["type"] == ["string", "null"]
    assert out["required"] == ["key"]


def test_nested_optional_is_simplified_too():
    schema = {
        "type": "object",
        "properties": {
            "outer": {
                "type": "object",
                "properties": {"inner": {"anyOf": [{"type": "integer"}, {"type": "null"}]}},
            }
        },
    }
    out = simplify_legacy_schema(schema)
    assert out is not None
    assert out["properties"]["outer"]["properties"]["inner"]["type"] == ["integer", "null"]


def test_simplified_schema_accepts_the_results_the_server_sends():
    """A legacy client validates results against the simplified schema. Null must pass."""
    schema = simplify_legacy_schema(json_schema(Optional))
    assert schema is not None
    validate({"key": "k", "handle": None}, schema)
    validate({"key": "k", "handle": "h"}, schema)
    with pytest.raises(ValidationError):
        validate({"key": "k", "handle": 1}, schema)


def test_nullable_object_keeps_null():
    out = simplify_legacy_schema(json_schema(Wrapper))
    assert out is not None
    inner = out["properties"]["inner"]
    assert inner["type"] == ["object", "null"]
    assert inner["properties"]["x"]["type"] == "integer"
    validate({"inner": None}, out)


def test_nullable_enum_keeps_null_among_its_choices():
    schema = {
        "type": "object",
        "properties": {
            "state": {"anyOf": [{"type": "string", "enum": ["on", "off"]}, {"type": "null"}]},
        },
    }
    out = simplify_legacy_schema(schema)
    assert out is not None
    assert out["properties"]["state"]["type"] == ["string", "null"]
    assert out["properties"]["state"]["enum"] == ["on", "off", None]
    validate({"state": None}, out)


def test_nullable_constant_is_refused():
    """A const cannot also accept null. Refuse rather than publish a schema that rejects null."""
    schema = {
        "type": "object",
        "properties": {"x": {"anyOf": [{"const": "only"}, {"type": "null"}]}},
    }
    assert simplify_legacy_schema(schema) is None


def test_unconstrained_nullable_branch_stays_unconstrained():
    schema = {"type": "object", "properties": {"x": {"anyOf": [{}, {"type": "null"}]}}}
    out = simplify_legacy_schema(schema)
    assert out is not None
    assert "type" not in out["properties"]["x"]
    validate({"x": None}, out)


def test_a_real_union_is_still_refused():
    """Removing null must not select an arbitrary remaining union branch."""
    schema = {
        "type": "object",
        "properties": {"x": {"anyOf": [{"type": "string"}, {"type": "integer"}]}},
    }
    assert simplify_legacy_schema(schema) is None


class Described(BaseModel):
    inner: Inner
    label: str = "x"


async def described(args: Described) -> Inner:
    """A tool with a schema worth simplifying."""
    return args.inner


def adapters() -> dict:
    from aiohttp_tiny_mcp.protocol.selection import AdapterSet

    return {adapter.version: adapter for adapter in AdapterSet.default().adapters}


def spec_for(fn):
    from aiohttp_tiny_mcp.server.specs import ToolSpec

    return ToolSpec.build(fn)


def test_every_revision_simplifies_a_tool_once(monkeypatch):
    """`describe_tool` caches per revision, so a hot `tools/list` does no schema work."""
    from aiohttp_tiny_mcp.protocol import v2025_11_25

    calls = 0
    original = v2025_11_25.simplify_legacy_schema

    def counted(schema):
        nonlocal calls
        calls += 1
        return original(schema)

    monkeypatch.setattr(v2025_11_25, "simplify_legacy_schema", counted)
    spec = spec_for(described)
    for adapter in adapters().values():
        first = adapter.describe_tool(spec)
        after_first = calls
        for _ in range(5):
            assert adapter.describe_tool(spec) is first
        assert calls == after_first, f"{adapter.version} simplified the schema again"
    assert calls > 0, "the legacy path was never reached"


def test_the_cached_definition_matches_an_uncached_build():
    spec = spec_for(described)
    for adapter in adapters().values():
        assert adapter.describe_tool(spec) == adapter.build_tool(spec)


def test_a_replaced_tool_is_described_again():
    """Each registration builds a new spec, which carries its own cache."""
    old, new = spec_for(described), spec_for(described)
    for adapter in adapters().values():
        assert adapter.describe_tool(old) is not adapter.describe_tool(new)
        assert adapter.describe_tool(old) == adapter.describe_tool(new)


def test_a_hidden_tool_stays_hidden_without_rebuilding():
    """A revision that hides a tool caches the None, rather than retrying every request."""
    from aiohttp_tiny_mcp.server.specs import ToolSpec

    spec = ToolSpec.build(described, min_revision="2026-07-28")
    legacy = adapters()["2024-11-05"]
    assert legacy.describe_tool(spec) is None
    assert legacy.version in spec.described
    assert adapters()["2026-07-28"].describe_tool(spec) is not None
