"""JSON Schema generation and simplification for legacy clients."""

from __future__ import annotations

import copy
import re
from typing import Any

from pydantic import BaseModel

HEADER_ANNOTATION = "x-mcp-header"
HEADER_NAME = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
HeaderParam = tuple[tuple[str, ...], str]


def json_schema(model: type[BaseModel]) -> dict:
    """Generate JSON Schema 2020-12 with refs intact; legacy adapters simplify it separately."""
    schema = model.model_json_schema()
    schema.setdefault("$schema", "https://json-schema.org/draft/2020-12/schema")
    schema.setdefault("type", "object")
    schema.setdefault("properties", {})
    return schema


def inline_refs(schema: dict[str, Any]) -> dict[str, Any]:
    """Inline refs for legacy clients. Recursive schemas are not supported."""
    defs = schema.get("$defs", {})

    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str) and ref.startswith("#/$defs/"):
                target = walk(copy.deepcopy(defs[ref.rsplit("/", 1)[1]]))
                return {**target, **{k: v for k, v in node.items() if k != "$ref"}}
            return {k: walk(v) for k, v in node.items() if k != "$defs"}
        if isinstance(node, list):
            return [walk(x) for x in node]
        return node

    return walk(schema)


def header_params(schema: dict[str, Any]) -> tuple[HeaderParam, ...]:
    """Return (property_path, header_suffix) pairs for reachable x-mcp-header declarations. Reject
    invalid declarations at registration.
    """
    found: list[HeaderParam] = []
    seen: set[str] = set()
    definitions = schema.get("$defs", {})

    def walk(node: Any, path: tuple[str, ...], active_refs: frozenset[str] = frozenset()) -> None:
        if not isinstance(node, dict):
            return
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/$defs/") and ref not in active_refs:
            target = definitions.get(ref.rsplit("/", 1)[1])
            if isinstance(target, dict):
                walk(
                    {**target, **{key: value for key, value in node.items() if key != "$ref"}},
                    path,
                    active_refs | {ref},
                )
                return
        properties = node.get("properties")
        if not isinstance(properties, dict):
            return
        for property_name, property_schema in properties.items():
            if not isinstance(property_schema, dict):
                continue
            property_path = (*path, property_name)
            annotation = property_schema.get(HEADER_ANNOTATION)
            if annotation is not None:
                if (
                    not isinstance(annotation, str)
                    or not annotation
                    or not HEADER_NAME.fullmatch(annotation)
                ):
                    raise ValueError(f"invalid x-mcp-header value at {'.'.join(property_path)}")
                if property_schema.get("type") not in {"string", "integer", "boolean"}:
                    raise ValueError(
                        f"x-mcp-header requires string, integer, or boolean at "
                        f"{'.'.join(property_path)}"
                    )
                folded = annotation.casefold()
                if folded in seen:
                    raise ValueError(f"duplicate x-mcp-header value: {annotation}")
                seen.add(folded)
                found.append((property_path, annotation))
            walk(property_schema, property_path, active_refs)

    walk(schema, ())
    return tuple(found)


def simplify_legacy_schema(schema: dict) -> dict | None:
    """Best-effort schema simplification for pre-2026-07-28 clients."""
    node = _simplify_node(inline_refs(schema))
    if node is None or node.get("type") != "object":
        return None
    return node


def _simplify_node(node: Any) -> Any:
    if not isinstance(node, dict):
        return node
    node = {k: v for k, v in node.items() if k not in ("if", "then", "else")}
    any_of = node.get("anyOf")
    if isinstance(any_of, list):
        branches = [
            branch
            for branch in any_of
            if not (isinstance(branch, dict) and branch.get("type") == "null")
        ]
        if len(branches) != 1:
            return None
        branch = _simplify_node(branches[0])
        if branch is None:
            return None
        node = {**{k: v for k, v in node.items() if k != "anyOf"}, **branch}
    if "oneOf" in node:
        return None
    properties = node.get("properties")
    if isinstance(properties, dict):
        simplified = {}
        for key, prop in properties.items():
            prop = _simplify_node(prop)
            if prop is None:
                return None
            simplified[key] = prop
        node = {**node, "properties": simplified}
    return node
