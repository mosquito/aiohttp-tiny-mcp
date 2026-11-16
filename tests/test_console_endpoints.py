from __future__ import annotations

import json
from enum import Enum
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Literal

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from pydantic import BaseModel, Field, RootModel

from aiohttp_tiny_mcp.console import Console, ConsoleEndpoint


class EndpointAttributes(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.attributes: dict[str, str | None] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "html":
            self.attributes = dict(attrs)


class Region(str, Enum):
    EUROPE = "eu"
    AMERICA = "us"


class Query(BaseModel):
    tenant: str = Field(description="Organization name", min_length=1)
    region: Region = Region.EUROPE
    count: int = Field(default=20, alias="page-size", ge=1, le=100)
    active: bool = False
    tags: list[Literal["red", "blue"]] = []
    ratio: float | None = None


def test_declarations_preserve_query_fields_and_inline_enum_refs() -> None:
    declaration = ConsoleEndpoint("/mcp?fixed=one", "Tenant tools", Query)
    configured = Console(
        "/unused", endpoints=[declaration, ConsoleEndpoint("/admin", "Admin tools")]
    )
    metadata = json.loads(configured.substitutions("/console")["ENDPOINTS"])
    assert configured.endpoint_path == "/mcp?fixed=one"
    assert [entry["path"] for entry in metadata] == ["/mcp?fixed=one", "/admin"]
    query = metadata[0]["query"]
    assert query["required"] == ["tenant"]
    assert query["properties"]["tenant"]["description"] == "Organization name"
    assert query["properties"]["region"]["enum"] == ["eu", "us"]
    assert query["properties"]["region"]["default"] == "eu"
    assert query["properties"]["page-size"]["minimum"] == 1
    assert query["properties"]["page-size"]["maximum"] == 100
    assert query["properties"]["active"]["default"] is False
    assert query["properties"]["tags"]["items"]["enum"] == ["red", "blue"]
    assert query["properties"]["ratio"]["default"] is None
    assert "$ref" not in json.dumps(query)
    assert metadata[1]["query"] == {"type": "object", "properties": {}}
    assert metadata[0]["auth"] is None
    assert metadata[1]["auth"] is None


@pytest.mark.parametrize("endpoints", [None, []])
def test_no_declarations_preserves_manual_endpoint(endpoints: Any) -> None:
    configured = Console("/custom?mode=manual", endpoints=endpoints)
    assert configured.endpoint_path == "/custom?mode=manual"
    assert json.loads(configured.substitutions("/console")["ENDPOINTS"]) == []


async def test_declared_metadata_is_escaped_and_does_not_register_routes(tmp_path: Path) -> None:
    template = tmp_path / "console.html"
    template.write_text(
        '<html data-endpoint="{{ENDPOINT}}" data-endpoints="{{ENDPOINTS}}"></html>',
        encoding="utf-8",
    )
    description = '<script>alert("unsafe")</script> & details'
    configured = Console(
        endpoints=[ConsoleEndpoint("/mcp?label=a&other=b", description, Query)],
        template=template,
    )
    async with TestClient(TestServer(configured.setup(web.Application()))) as client:
        response = await client.get("/console")
        assert response.status == 200
        text = await response.text()
        assert "<script>" not in text
        assert "&lt;script&gt;" in text
        parser = EndpointAttributes()
        parser.feed(text)
        metadata = json.loads(parser.attributes["data-endpoints"] or "[]")
        assert metadata[0]["description"] == description
        assert parser.attributes["data-endpoint"] == "/mcp?label=a&other=b"
        assert (await client.get("/mcp")).status == 404


@pytest.mark.parametrize(
    "path",
    [
        "mcp",
        "https://other.example/mcp",
        "//other.example/mcp",
        "/\\other.example/mcp",
        "/mcp#fragment",
        "/mcp#",
        "/mcp\n?token=hidden",
        "/mcp\r",
        "/mcp\t",
        "/mcp\x00",
        "/mcp\x7f",
    ],
)
def test_invalid_declared_paths_are_rejected(path: str) -> None:
    with pytest.raises(ValueError, match="root-relative"):
        Console(endpoints=[ConsoleEndpoint(path, "Tools")])


def test_duplicate_paths_and_empty_descriptions_are_rejected() -> None:
    with pytest.raises(ValueError, match="unique"):
        Console(endpoints=[ConsoleEndpoint("/mcp", "One"), ConsoleEndpoint("/mcp", "Two")])
    with pytest.raises(ValueError, match="description"):
        Console(endpoints=[ConsoleEndpoint("/mcp", " ")])


def test_nested_and_ambiguous_query_fields_are_rejected() -> None:
    class Nested(BaseModel):
        name: str

    class NestedQuery(BaseModel):
        nested: Nested

    class AmbiguousQuery(BaseModel):
        value: int | str

    class NestedListQuery(BaseModel):
        values: list[list[str]]

    class ArbitraryQuery(BaseModel):
        value: Any

    for query in (NestedQuery, AmbiguousQuery, NestedListQuery, ArbitraryQuery):
        with pytest.raises(ValueError, match="Query parameter"):
            Console(endpoints=[ConsoleEndpoint("/mcp", "Tools", query)])


def test_recursive_and_non_object_query_models_are_rejected() -> None:
    class RecursiveQuery(BaseModel):
        child: RecursiveQuery | None = None

    with pytest.raises(ValueError, match="Recursive"):
        Console(endpoints=[ConsoleEndpoint("/mcp", "Tools", RecursiveQuery)])
    with pytest.raises(ValueError, match="named fields"):
        Console(endpoints=[ConsoleEndpoint("/mcp", "Tools", RootModel[list[str]])])


def test_query_must_be_a_model_class() -> None:
    with pytest.raises(ValueError, match="model class"):
        Console(endpoints=[ConsoleEndpoint("/mcp", "Tools", "Query")])  # type: ignore[arg-type]


def test_authentication_declarations_preserve_supported_methods_and_order() -> None:
    configured = Console(
        endpoints=[
            ConsoleEndpoint("/public", "Public tools", auth=("none",)),
            ConsoleEndpoint("/private", "Private tools", auth=("bearer", "query")),
            ConsoleEndpoint("/custom", "Other tools", auth=("basic", "custom", "oauth")),
        ],
        oauth_client_id="console-client",
    )
    metadata = json.loads(configured.substitutions("/console")["ENDPOINTS"])
    assert [entry["auth"] for entry in metadata] == [
        ["none"],
        ["bearer", "query"],
        ["basic", "custom", "oauth"],
    ]


@pytest.mark.parametrize("auth", [[], ["unsupported"], ["bearer", "bearer"], "none"])
def test_invalid_authentication_declarations_are_rejected(auth: Any) -> None:
    with pytest.raises(ValueError, match="auth"):
        Console(endpoints=[ConsoleEndpoint("/mcp", "Tools", auth=auth)])


def test_declared_oauth_requires_client_configuration() -> None:
    with pytest.raises(ValueError, match="oauth_client_id"):
        Console(endpoints=[ConsoleEndpoint("/mcp", "Tools", auth=("oauth",))])
