"""Console choice fields preserve labels, defaults, and JSON values."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

NODE = shutil.which("node")
CONSOLE = Path(__file__).parent.parent / "src/aiohttp_tiny_mcp/console/console.js"
pytestmark = [pytest.mark.skipif(NODE is None, reason="node is not installed")]

# Model the DOM operations used by form fields, including select defaults.
DOM = """
function element(tag, className, text) {
  return {
    tag, className, text, dataset: {}, children: [], checked: false, _value: undefined,
    validity: {badInput: false},
    get valueAsNumber() { return this.value === "" ? NaN : Number(this.value); },
    listeners: {},
    addEventListener(name, callback) { this.listeners[name] = callback; },
    get options() { return this.children; },
    setAttribute(name, value) { this[name] = value; },
    append(...children) { this.children.push(...children); },
    set value(value) { this._value = String(value); },
    get value() {
      if (this.tag === "select") {
        if (this._value === undefined) return this.children[0]?.value || "";
        return this.children.some(child => child.value === this._value) ? this._value : "";
      }
      return this._value ?? (this.tag === "option" ? this.text : "");
    }
  };
}
"""


def render(property, *, required=False, selected=None, null_states=(), checked_choices=None):
    source = CONSOLE.read_text()
    functions = source[source.index("function buildFields(") : source.index("function show(")]
    driver = (
        DOM
        + functions
        + f"""
const into = element("div");
const fields = buildFields({{
  properties: {{value: {json.dumps(property)}}},
  required: {json.dumps(["value"] if required else [])}
}}, into);
const input = fields[0];
const selected = {json.dumps(selected)};
if (selected !== null) input.value = input.children[selected].value;
const checkedChoices = {json.dumps(checked_choices)};
if (checkedChoices !== null) input.choiceInputs.forEach((option, index) => {{
  option.checked = checkedChoices.includes(index);
}});
for (const checked of {json.dumps(null_states)}) {{
  input.nullToggle.checked = checked;
  input.nullToggle.onchange();
}}
process.stdout.write(JSON.stringify({{
  disabled: input.disabled || false,
  nullable: !!input.nullToggle,
  heading: into.children[0].children[0].className,
  tag: input.tag,
  step: input.step,
  min: input.min,
  max: input.max,
  labels: input.children.map(child => child.text),
  values: readFields(fields),
  missing: missing(fields).length,
  rowClass: into.children[0].className
}}));
"""
    )
    result = subprocess.run(
        [NODE, "-e", driver], capture_output=True, text=True, check=True, timeout=10
    )
    return json.loads(result.stdout)


def test_named_oneof_choices_and_default():
    result = render(
        {
            "oneOf": [{"const": "a", "title": "Alpha"}, {"const": "b", "title": "Beta"}],
            "default": "b",
        },
        required=True,
    )
    assert result["tag"] == "select"
    assert result["labels"] == ["Alpha", "Beta"]
    assert result["values"] == {"value": "b"}
    assert result["missing"] == 0


@pytest.mark.parametrize("value", ["", "  spaced  ", 0, 1.5, False, True, None, [1], {"a": 1}])
def test_oneof_preserves_json_values(value):
    result = render({"oneOf": [{"const": value}]}, required=True)
    assert result["values"] == {"value": value}
    assert result["missing"] == 0


def test_optional_choice_can_be_omitted_or_selected():
    schema = {"type": "boolean", "oneOf": [{"enum": [False], "title": "No"}]}
    assert render(schema)["values"] == {}
    result = render(schema, selected=1)
    assert result["values"] == {"value": False}
    assert result["rowClass"] == "argument"
    assert result["labels"] == ["", "No"]


@pytest.mark.parametrize("value", [False, 0, "", None, "0"])
def test_enum_uses_typed_choices(value):
    result = render({"enum": ["other", value], "default": value}, required=True)
    assert result["tag"] == "select"
    assert result["values"] == {"value": value}


def test_select_change_preserves_type():
    result = render({"oneOf": [{"const": 1}, {"const": "1"}]}, required=True, selected=1)
    assert result["values"] == {"value": "1"}


@pytest.mark.parametrize(
    "branches", [[], [{"type": "string"}], [{"const": "a"}, {"type": "string"}]]
)
def test_nonconstant_oneof_keeps_existing_input(branches):
    assert render({"oneOf": branches})["tag"] == "input"


@pytest.mark.parametrize(
    "kind,default",
    [("string", "text"), ("integer", 3), ("boolean", False), ("array", [1]), ("object", {"a": 1})],
)
@pytest.mark.parametrize("style", ["type", "anyOf", "oneOf"])
def test_nullable_toggle_disables_and_restores_value(kind, default, style):
    schema = (
        {"type": [kind, "null"]} if style == "type" else {style: [{"type": kind}, {"type": "null"}]}
    )
    schema["default"] = default
    checked = render(schema, required=True, null_states=[True])
    assert checked["values"] == {"value": None}
    assert checked["disabled"] is True
    assert checked["missing"] == 0
    assert checked["heading"] == "argument-heading"
    restored = render(schema, required=True, null_states=[True, False])
    assert restored["values"] == {"value": default}
    assert restored["disabled"] is False


def test_nullable_array_default_and_absence():
    schema = {
        "anyOf": [{"type": "array", "items": {"type": "string"}}, {"type": "null"}],
        "default": None,
    }
    assert render(schema)["values"] == {"value": None}
    assert render(schema)["disabled"] is True
    assert render(schema, null_states=[False])["values"] == {}
    assert render(schema, required=True, null_states=[False])["missing"] == 1


def test_nullable_enum_and_named_choices():
    for branch in [
        {"type": "string", "enum": ["a", "b"]},
        {"oneOf": [{"const": "a", "title": "Alpha"}, {"const": "b", "title": "Beta"}]},
    ]:
        schema = {"anyOf": [branch, {"type": "null"}], "default": None}
        assert render(schema)["tag"] == "select"
        assert render(schema)["values"] == {"value": None}
        assert render(schema, selected=2, null_states=[False])["values"] == {"value": "b"}


def test_oneof_null_type_and_constants():
    schema = {"oneOf": [{"const": "a", "title": "Alpha"}, {"type": "null"}]}
    assert render(schema, required=True)["tag"] == "select"
    assert render(schema, required=True)["values"] == {"value": "a"}
    assert render(schema, null_states=[True])["values"] == {"value": None}


def test_nonnullable_field_has_no_toggle():
    assert render({"type": "string"})["nullable"] is False


@pytest.mark.parametrize(
    "items",
    [
        {"enum": ["tasks", "comments", "memory"]},
        {
            "oneOf": [
                {"const": "tasks", "title": "Tasks"},
                {"const": "comments"},
                {"const": "memory"},
            ]
        },
    ],
)
def test_array_choices_defaults_changes_and_empty(items):
    schema = {"type": "array", "items": items, "default": ["tasks", "memory"]}
    assert render(schema)["tag"] == "fieldset"
    assert render(schema)["values"] == {"value": ["tasks", "memory"]}
    assert render(schema, checked_choices=[1])["values"] == {"value": ["comments"]}
    assert render(schema, required=True, checked_choices=[])["values"] == {"value": []}


def test_nullable_array_choices_restore_and_types():
    schema = {
        "anyOf": [{"type": "array", "items": {"enum": [0, False, "", None]}}, {"type": "null"}],
        "default": None,
    }
    assert render(schema)["values"] == {"value": None}
    result = render(schema, checked_choices=[0, 1, 2, 3], null_states=[False, True, False])
    assert result["values"] == {"value": [0, False, "", None]}
    assert result["disabled"] is False


def test_simplified_nullable_schema_uses_null_default():
    result = render({"type": "array", "items": {"enum": ["todo", "done"]}, "default": None})
    assert result["tag"] == "fieldset"
    assert result["nullable"] is True
    assert result["disabled"] is True
    assert result["values"] == {"value": None}


@pytest.mark.parametrize("kind,step,value", [("number", "any", 0.26), ("integer", "1", 1)])
def test_numeric_step_and_bounds(kind, step, value):
    for schema in [
        {"type": kind, "minimum": -1, "maximum": 1},
        {"anyOf": [{"type": kind, "minimum": -1, "maximum": 1}, {"type": "null"}]},
    ]:
        schema["default"] = value
        result = render(schema)
        assert result["step"] == step
        assert result["min"] == -1
        assert result["max"] == 1
        assert result["values"] == {"value": value}
