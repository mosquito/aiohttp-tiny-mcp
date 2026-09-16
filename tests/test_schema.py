from __future__ import annotations

import pytest
from pydantic import BaseModel, Field

from aiohttp_tiny_mcp.schema import header_params, inline_refs, json_schema, simplify_legacy_schema


class Inner(BaseModel):
    x: int


class Outer(BaseModel):
    inner: Inner
    name: str = Field(description="a name", json_schema_extra={"x-mcp-header": "Name"})


class Routing(BaseModel):
    region: str = Field(json_schema_extra={"x-mcp-header": "Region"})


class Routed(BaseModel):
    routing: Routing


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
    assert out["properties"]["handle"]["type"] == "string"
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
    assert out["properties"]["outer"]["properties"]["inner"]["type"] == "integer"


def test_a_real_union_is_still_refused():
    """Removing null must not select an arbitrary remaining union branch."""
    schema = {
        "type": "object",
        "properties": {"x": {"anyOf": [{"type": "string"}, {"type": "integer"}]}},
    }
    assert simplify_legacy_schema(schema) is None
