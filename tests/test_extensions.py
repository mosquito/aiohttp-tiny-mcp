from __future__ import annotations

import json

import pytest
from aiohttp.test_utils import TestClient, TestServer
from pydantic import BaseModel, ConfigDict

from aiohttp_tiny_mcp import (
    ClientError,
    Endpoint,
    Exchange,
    Extension,
    Registry,
    TextResourceContents,
)
from aiohttp_tiny_mcp.models import CacheableResult
from aiohttp_tiny_mcp.testing import connect, over_http


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: int


class Nothing(BaseModel):
    pass


class Result(CacheableResult):
    doubled: int


@pytest.mark.parametrize("transport", [connect, over_http])
async def test_extension_methods_use_metadata_validation_and_dependencies(transport):
    registry = Registry("extensions", "1")
    released = []

    async def provide(ex):
        try:
            yield "dependency"
        finally:
            released.append(True)

    registry.provide(str, provide)
    extension = Extension("example.org/arithmetic", capabilities={"feature": True})

    @extension.method("arithmetic/double")
    async def double(args: Input, ex: Exchange, dependency: str) -> Result:
        assert ex.client_info.name == "aiohttp-tiny-mcp-client"
        assert dependency == "dependency"
        return Result(doubled=args.value * 2)

    registry.extension(extension)
    async with transport(registry, adapter="2026-07-28") as client:
        discovery = await client.initialize()
        assert discovery["capabilities"]["extensions"] == {
            "example.org/arithmetic": {"feature": True}
        }
        assert "resources" not in discovery["capabilities"]
        result = await client.request_method("arithmetic/double", {"value": 7})
        assert result["doubled"] == 14
        assert result["resultType"] == "complete"
        assert result["ttlMs"] == 0
        assert result["cacheScope"] == "private"
        assert result["_meta"]["io.modelcontextprotocol/serverInfo"]["name"] == "extensions"
        assert released == [True]
        with pytest.raises(ClientError) as bad:
            await client.request_method("arithmetic/double", {"value": "bad"})
        assert bad.value.code == -32602
        with pytest.raises(ClientError) as unknown:
            await client.request_method("arithmetic/unknown")
        assert unknown.value.code == -32601


@pytest.mark.parametrize("transport", [connect, over_http])
@pytest.mark.parametrize("version", ["2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25"])
async def test_legacy_extensions_publish_prefixed_resources_and_manifest(transport, version):
    extension = Extension("example.org/manual")

    @extension.method("manual/get")
    async def get(args: Nothing):
        return {"text": "manual"}

    @extension.resource("manual://guide/start.md", mime_type="text/markdown")
    async def read(args: Nothing):
        return TextResourceContents(uri="manual://guide/start.md", text="# Guide")

    registry = Registry("legacy", "1")
    registry.extension(extension)
    prefix = "mcp-extensions://example.org/manual/"
    async with transport(registry, adapter=version) as client:
        discovery = await client.initialize()
        assert "extensions" not in discovery["capabilities"]
        assert {item.uri for item in await client.list_resources()} == {
            prefix + "manifest.json",
            prefix + "guide/start.md",
            prefix + "manual/get",
        }
        result = await client.read_resource(prefix + "guide/start.md")
        assert result.contents[0].text == "# Guide"
        assert result.contents[0].uri == prefix + "guide/start.md"
        manifest = await client.read_resource(prefix + "manifest.json")
        details = json.loads(manifest.contents[0].text)
        assert details["name"] == "example.org/manual"
        assert details["capabilities"] == {}
        assert details["methods"] == ["manual/get"]
        assert details["resources"] == [prefix + "guide/start.md", prefix + "manual/get"]
        assert details["methodResources"]["manual/get"]["uri"] == prefix + "manual/get"
        invoked = await client.read_resource(prefix + "manual/get")
        assert json.loads(invoked.contents[0].text) == {"text": "manual"}
        with pytest.raises(ClientError) as missing:
            await client.request_method("manual/get")
        assert missing.value.code == -32601
        with pytest.raises(ClientError):
            await client.read_resource("manual://guide/start.md")


def test_registration_is_atomic_and_reserves_protocol_methods():
    registry = Registry("registration", "1")

    async def read(args: Nothing):
        return {}

    for name in ("tools/list", "initialize", "notifications/cancelled"):
        extension = Extension("example.org/conflict")
        extension.method(name, read)
        with pytest.raises(ValueError, match="protocol method"):
            registry.extension(extension)
        assert not registry.extensions
        assert not registry.resources_fixed

    first = Extension("example.org/first")
    first.method("custom/read", read)
    registry.extension(first)
    second = Extension("example.org/second")
    second.method("custom/read", read)
    with pytest.raises(ValueError, match="duplicate extension method"):
        registry.extension(second)
    with pytest.raises(ValueError, match="duplicate extension:"):
        registry.extension(first)


async def test_extension_is_reusable_and_declarations_are_snapshots():
    extension = Extension("example.org/snapshot", capabilities={"settings": {"v": 1}})

    @extension.method("snapshot/read")
    async def read(args: Nothing):
        return {"value": "original"}

    one, two = Registry("one", "1"), Registry("two", "1")
    one.extension(extension)
    two.extension(extension)
    extension.capabilities["settings"]["v"] = 2
    extension.methods.clear()
    for registry in (one, two):
        async with connect(registry, adapter="2026-07-28") as client:
            discovery = await client.initialize()
            assert discovery["capabilities"]["extensions"][extension.name]["settings"]["v"] == 1
            assert (await client.request_method("snapshot/read"))["value"] == "original"


async def test_unregistered_and_future_extensions_remain_unavailable():
    registry = Registry("future", "1")
    extension = Extension("example.org/future", min_revision="2027-01-01")

    @extension.method("future/read")
    async def read(args: Nothing):
        return {}

    registry.extension(extension)
    async with connect(registry, adapter="2026-07-28") as client:
        assert "extensions" not in (await client.initialize())["capabilities"]
        with pytest.raises(ClientError) as missing:
            await client.request_method("future/read")
        assert missing.value.code == -32601


async def test_extensions_keep_modern_http_header_and_metadata_checks():
    registry = Registry("headers", "1")
    extension = Extension("example.org/headers")
    calls = []

    @extension.method("headers/read")
    async def read(args: Nothing):
        calls.append(True)
        return {}

    registry.extension(extension)
    async with TestClient(TestServer(Endpoint(registry).app())) as client:
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "headers/read",
            "params": {
                "_meta": {
                    "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                    "io.modelcontextprotocol/clientCapabilities": {},
                }
            },
        }
        headers = {"MCP-Protocol-Version": "2026-07-28", "Mcp-Method": "wrong"}
        result = await client.post("/mcp", json=body, headers=headers)
        assert result.status == 400
        assert (await result.json())["error"]["code"] == -32020
        headers["Mcp-Method"] = "headers/read"
        body["params"]["_meta"].pop("io.modelcontextprotocol/clientCapabilities")
        result = await client.post("/mcp", json=body, headers=headers)
        assert (await result.json())["error"]["code"] == -32600
        assert not calls


def test_resource_conflicts_and_missing_providers_leave_registry_unchanged():
    async def read(args: Nothing):
        return ""

    registry = Registry("conflicts", "1")
    registry.resource("mcp-extensions://example.org/data/file", read)
    extension = Extension("example.org/data")
    extension.resource("data://file", read)
    with pytest.raises(ValueError, match="duplicate resource"):
        registry.extension(extension)
    assert not registry.extensions
    assert "data://file" not in registry.resources_fixed

    other = Extension("example.org/deps")

    @other.method("deps/read")
    async def missing(args: Nothing, dependency: str):
        return {}

    with pytest.raises(TypeError, match="no provider"):
        registry.extension(other)
    assert not registry.extensions
