"""Resource scope declarations require authentication and survive extension snapshots."""

from collections.abc import Iterable

import pytest
from pydantic import BaseModel

from aiohttp_tiny_mcp import Extension, Registry, StaticBasicAuth
from aiohttp_tiny_mcp.server.specs import ResourceSpec


class Nothing(BaseModel):
    pass


class Entry(BaseModel):
    id: int


async def fixed(args: Nothing) -> str:
    return "private configuration"


async def entry(args: Entry) -> str:
    return str(args.id)


@pytest.mark.parametrize("templated", [False, True])
def test_resource_scope_declaration_is_an_immutable_snapshot(templated: bool):
    registry = Registry("scoped", "1", auth=StaticBasicAuth(("reader", "secret", ["files:read"])))
    uri = "files://entries/{id}" if templated else "files://config"
    required = {"files:read"}
    registry.resource(uri, entry if templated else fixed, scopes=required)
    required.add("files:write")

    matched = registry.match_resource("files://entries/42" if templated else uri)
    assert matched is not None
    spec, variables = matched
    assert spec.scopes == frozenset({"files:read"})
    assert variables == ({"id": "42"} if templated else {})
    definition = spec.template if templated else spec.definition
    assert definition is not None
    assert "scopes" not in definition.wire()


@pytest.mark.parametrize("templated", [False, True])
def test_scoped_resource_registration_requires_authentication(templated: bool):
    registry = Registry("scoped", "1")
    uri = "files://entries/{id}" if templated else "files://config"
    with pytest.raises(TypeError, match="scopes were declared.*no auth="):
        registry.resource(uri, entry if templated else fixed, scopes={"files:read"})
    assert not registry.resources_fixed
    assert not registry.resources_templated


@pytest.mark.parametrize("templated", [False, True])
@pytest.mark.parametrize("scopes", [None, (), frozenset()])
def test_unscoped_resources_need_no_authentication(templated: bool, scopes: Iterable[str] | None):
    registry = Registry("public", "1")
    uri = "files://entries/{id}" if templated else "files://config"
    registry.resource(uri, entry if templated else fixed, scopes=scopes)
    matched = registry.match_resource("files://entries/42" if templated else uri)
    assert matched is not None
    assert matched[0].scopes == frozenset()


@pytest.mark.parametrize("templated", [False, True])
def test_resource_build_accepts_scope_iterators(templated: bool):
    uri = "files://entries/{id}" if templated else "files://config"
    scopes = iter(["files:read", "files:read", "profile:read"])
    spec = ResourceSpec.build(uri, entry if templated else fixed, scopes=scopes)
    assert spec.scopes == frozenset({"files:read", "profile:read"})


@pytest.mark.parametrize("templated", [False, True])
def test_extension_resource_aliases_keep_scope_requirements(templated: bool):
    extension = Extension("example/files")
    uri = "files://entries/{id}" if templated else "files://config"
    extension.resource(uri, entry if templated else fixed, scopes={"files:read"})
    registry = Registry("scoped", "1", auth=StaticBasicAuth(("reader", "secret", ["files:read"])))
    registry.extension(extension)
    extension.resources[uri].scopes = frozenset({"changed"})

    path = "entries/42" if templated else "config"
    for address in (f"files://{path}", f"mcp-extensions://example/files/{path}"):
        matched = registry.match_resource(address)
        assert matched is not None
        assert matched[0].scopes == frozenset({"files:read"})


@pytest.mark.parametrize("templated", [False, True])
def test_scoped_extension_registration_requires_authentication(templated: bool):
    extension = Extension("example/files")
    uri = "files://entries/{id}" if templated else "files://config"
    extension.resource(uri, entry if templated else fixed, scopes={"files:read"})
    registry = Registry("scoped", "1")
    with pytest.raises(TypeError, match="scopes were declared.*no auth="):
        registry.extension(extension)
    assert not registry.extensions
    assert not registry.resources_fixed
    assert not registry.resources_templated
    assert not registry.resource_aliases
