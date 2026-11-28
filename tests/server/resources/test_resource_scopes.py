"""Resource scopes protect contents and completions before dependency resolution."""

from collections.abc import Iterable

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from pydantic import BaseModel

from aiohttp_tiny_mcp import Endpoint, Exchange, Extension, Registry, SseEndpoint
from aiohttp_tiny_mcp.auth import Authorization, Principal, StaticVerifier
from aiohttp_tiny_mcp.client.base import ClientError
from aiohttp_tiny_mcp.protocol.core import Operation
from aiohttp_tiny_mcp.protocol.models import CompleteParams, ListParams
from aiohttp_tiny_mcp.protocol.selection import AdapterSet
from aiohttp_tiny_mcp.server.specs import ResourceSpec
from aiohttp_tiny_mcp.testing import connect, over_http
from tests.helpers import OldClient
from tests.server.resources.test_subscriptions import message

VERSIONS = tuple(AdapterSet.default().by_version)


class Nothing(BaseModel):
    pass


class Entry(BaseModel):
    id: int


class Dependency:
    pass


def registry_with_resources(scopes: Iterable[str] = ()) -> tuple[Registry, list[str]]:
    registry = Registry(
        "resources",
        "1",
        auth=Authorization(
            verifier=StaticVerifier(
                {"test-token": Principal(subject="reader", scopes=frozenset(scopes))}
            ),
            resource="https://example.test/mcp",
        ),
    )
    calls: list[str] = []

    async def dependency(ex: Exchange) -> Dependency:
        calls.append("dependency")
        return Dependency()

    registry.provide(Dependency, dependency)

    @registry.resource("files://config", scopes={"files:read", "profile:read"})
    async def fixed(args: Nothing, dependency: Dependency) -> str:
        calls.append("fixed")
        return "private configuration"

    @registry.resource("files://entries/{id}", scopes={"files:read", "profile:read"})
    async def entry(args: Entry, dependency: Dependency) -> str:
        calls.append("template")
        return str(args.id)

    @registry.completions
    async def complete(args: CompleteParams, dependency: Dependency) -> list[str]:
        calls.append("complete")
        return ["42"]

    return registry, calls


@pytest.mark.parametrize("version", VERSIONS)
@pytest.mark.parametrize("uri", ["files://config", "files://entries/42"])
async def test_missing_resource_scopes_prevent_handler_and_dependencies(version: str, uri: str):
    registry, calls = registry_with_resources({"profile:read"})
    async with over_http(
        registry, adapter=version, headers={"Authorization": "Bearer test-token"}
    ) as client:
        assert "files://config" in [item.uri for item in await client.list_resources()]
        templates = await client.request(Operation.LIST_RESOURCE_TEMPLATES, ListParams())
        assert templates["resourceTemplates"][0]["uriTemplate"] == "files://entries/{id}"
        with pytest.raises(ClientError, match="files:read") as denied:
            await client.read_resource(uri)
        assert denied.value.code == -32003
    assert calls == []


@pytest.mark.parametrize("version", VERSIONS)
@pytest.mark.parametrize(
    "uri,content", [("files://config", "private configuration"), ("files://entries/42", "42")]
)
async def test_resource_scopes_allow_contents(version: str, uri: str, content: str):
    registry, calls = registry_with_resources({"files:read", "profile:read"})
    async with over_http(
        registry, adapter=version, headers={"Authorization": "Bearer test-token"}
    ) as client:
        result = await client.read_resource(uri)
        assert result.contents[0].text == content
    assert calls[0] == "dependency"
    assert len(calls) == 2


@pytest.mark.parametrize("version", VERSIONS)
async def test_stdio_cannot_bypass_resource_scope_checks(version: str):
    registry, calls = registry_with_resources({"files:read", "profile:read"})
    async with connect(registry, adapter=version) as client:
        with pytest.raises(ClientError, match="no verified caller") as denied:
            await client.read_resource("files://entries/42")
        assert denied.value.code == -32003
    assert calls == []


@pytest.mark.parametrize("version", ["2025-11-25", "2026-07-28"])
@pytest.mark.parametrize("authorized", [False, True])
async def test_template_completions_require_resource_scopes(version: str, authorized: bool):
    registry, calls = registry_with_resources({"files:read", "profile:read"} if authorized else ())
    async with over_http(
        registry, adapter=version, headers={"Authorization": "Bearer test-token"}
    ) as client:
        ref = {"type": "ref/resource", "uri": "files://entries/{id}"}
        argument = {"name": "id", "value": "4"}
        if authorized:
            assert (await client.complete(ref, argument)).completion.values == ["42"]
            assert calls == ["dependency", "complete"]
        else:
            with pytest.raises(ClientError, match="files:read") as denied:
                await client.complete(ref, argument)
            assert denied.value.code == -32003
            assert calls == []


async def test_http_resource_scope_denial_has_permission_status():
    registry, calls = registry_with_resources()
    async with TestClient(TestServer(Endpoint(registry).app())) as client:
        response = await client.post(
            "/mcp",
            json=message(1, "resources/read", uri="files://config"),
            headers={
                "Authorization": "Bearer test-token",
                "Accept": "application/json",
                "MCP-Protocol-Version": "2026-07-28",
                "Mcp-Method": "resources/read",
                "Mcp-Name": "files://config",
            },
        )
        assert response.status == 403
        assert (await response.json())["error"]["code"] == -32003
    assert calls == []


async def test_http_sse_resource_scope_denial_is_a_protocol_error():
    registry, calls = registry_with_resources()
    async with TestServer(SseEndpoint(registry).setup(web.Application())) as server:
        client = OldClient(str(server.make_url("")), headers={"Authorization": "Bearer test-token"})
        try:
            await client.open()
            result = await client.call("resources/read", {"uri": "files://entries/42"})
            assert result["error"]["code"] == -32003
            assert "files:read" in result["error"]["message"]
        finally:
            await client.close()
    assert calls == []


@pytest.mark.parametrize("version", ["2025-11-25", "2026-07-28"])
@pytest.mark.parametrize("templated", [False, True])
async def test_extension_resource_scope_applies_to_revision_uri(version: str, templated: bool):
    registry, calls = registry_with_resources()
    extension = Extension("example/files")

    async def content(args: Entry, dependency: Dependency) -> str:
        calls.append("extension")
        return str(args.id)

    async def fixed(args: Nothing, dependency: Dependency) -> str:
        calls.append("extension")
        return "secret"

    extension.resource(
        "secret://{id}" if templated else "secret://fixed",
        content if templated else fixed,
        scopes={"secret:read"},
    )
    registry.extension(extension)
    suffix = "42" if templated else "fixed"
    uri = (
        f"secret://{suffix}"
        if version == "2026-07-28"
        else f"mcp-extensions://example/files/{suffix}"
    )
    async with over_http(
        registry, adapter=version, headers={"Authorization": "Bearer test-token"}
    ) as client:
        with pytest.raises(ClientError, match="secret:read") as denied:
            await client.read_resource(uri)
        assert denied.value.code == -32003
    assert calls == []


@pytest.mark.parametrize("version", ["2025-11-25", "2026-07-28"])
@pytest.mark.parametrize("authorized", [False, True])
async def test_hidden_extension_template_completion_does_not_run_dependencies(
    version: str, authorized: bool
):
    registry, calls = registry_with_resources({"secret:read"} if authorized else ())
    extension = Extension("example/files")

    @extension.resource("secret://{id}", scopes={"secret:read"})
    async def content(args: Entry) -> str:
        return str(args.id)

    registry.extension(extension)
    hidden = "mcp-extensions://example/files/{id}" if version == "2026-07-28" else "secret://{id}"
    async with over_http(
        registry, adapter=version, headers={"Authorization": "Bearer test-token"}
    ) as client:
        with pytest.raises(ClientError, match="resource not found") as denied:
            await client.complete(
                {"type": "ref/resource", "uri": hidden}, {"name": "id", "value": "4"}
            )
        assert denied.value.code == (-32602 if version == "2026-07-28" else -32002)
    assert calls == []


@pytest.mark.parametrize("version", ["2025-11-25", "2026-07-28"])
async def test_unknown_resource_completion_preserves_custom_handler(version: str):
    registry, calls = registry_with_resources()
    async with over_http(
        registry, adapter=version, headers={"Authorization": "Bearer test-token"}
    ) as client:
        result = await client.complete(
            {"type": "ref/resource", "uri": "external://entries/{id}"},
            {"name": "id", "value": "4"},
        )
        assert result.completion.values == ["42"]
    assert calls == ["dependency", "complete"]


@pytest.mark.parametrize("uri", ["files://config", "files://entries/{id}"])
def test_scoped_resources_reject_public_caching(uri: str):
    async def content(args: Nothing) -> str:
        return "private"

    with pytest.raises(ValueError, match="cannot use public caching"):
        ResourceSpec.build(uri, content, scopes={"files:read"}, cache_scope="public")


async def test_scoped_resource_cache_metadata_stays_private():
    registry, calls = registry_with_resources({"files:read", "profile:read"})
    registry.resources_fixed["files://config"].cache_ttl = 1
    async with over_http(
        registry, adapter="2026-07-28", headers={"Authorization": "Bearer test-token"}
    ) as client:
        result = await client.read_resource("files://config")
        assert result.cache_scope == "private"
        assert result.ttl_ms == 1000
    assert calls == ["dependency", "fixed"]
