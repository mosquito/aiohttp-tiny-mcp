"""Keep extension compatibility inside ordinary legacy MCP resource messages."""

import json
from urllib.parse import quote

import pytest
from pydantic import BaseModel

from aiohttp_tiny_mcp import Extension, Registry
from aiohttp_tiny_mcp.core import Operation
from aiohttp_tiny_mcp.testing import connect, over_http, pick

VERSIONS = ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25")
PREFIX = "mcp-extensions://example.org/jobs/"


class Nothing(BaseModel):
    pass


class Job(BaseModel):
    taskId: str
    inputResponses: dict


async def inspect_legacy_wire(transport, version):
    """Return captured messages for optional validation against upstream schemas."""
    extension = Extension("example.org/jobs")
    calls = []

    @extension.method("jobs/start")
    async def start(args: Nothing):
        calls.append("start")
        return {"resultType": "task", "ttlMs": 1000, "task": {"taskId": "a"}}

    @extension.method("tasks/update")
    async def update(args: Job):
        calls.append(args.inputResponses)
        return {"resultType": "complete"}

    adapter = pick(version)
    methods_before = {adapter.method_for(operation) for operation in Operation}
    registry = Registry("wire-audit", "1")
    registry.extension(extension)
    assert not adapter.supports_extensions
    assert {adapter.method_for(operation) for operation in Operation} == methods_before
    captured = []

    async with transport(registry, adapter=version) as client:
        initialized = await client.initialize()
        captured.append(("InitializeResult", initialized))
        assert set(initialized) == {"protocolVersion", "capabilities", "serverInfo"}
        assert initialized["protocolVersion"] == version
        assert set(initialized["capabilities"]) == {"resources", "logging"}

        async def exchange(method, params, request_type=None, result_type=None):
            envelope = {
                "jsonrpc": "2.0",
                "id": client.next_id(),
                "method": method,
                "params": params,
            }
            if request_type:
                captured.append((request_type, envelope))
            frames = []
            async for frame in client.exchange(envelope, method=method, name=params.get("uri")):
                frames.append(frame)
                if frame.get("id") == envelope["id"]:
                    break
            assert len(frames) == 1
            response = frames[0]
            assert response["id"] == envelope["id"]
            assert response["jsonrpc"] == "2.0"
            assert set(response) == {"jsonrpc", "id", "result" if result_type else "error"}
            captured.append(("JSONRPCResponse" if result_type else "JSONRPCError", response))
            if result_type:
                captured.append((result_type, response["result"]))
                return response["result"]
            assert response["error"]["code"] == -32601
            return response["error"]

        listed = await exchange("resources/list", {}, "ListResourcesRequest", "ListResourcesResult")
        assert set(listed) == {"resources"}
        assert {item["uri"] for item in listed["resources"]} == {
            PREFIX + "manifest.json",
            PREFIX + "jobs/start",
        }
        for item in listed["resources"]:
            assert set(item) == {"uri", "name", "description", "mimeType"}
        templates = await exchange(
            "resources/templates/list",
            {},
            "ListResourceTemplatesRequest",
            "ListResourceTemplatesResult",
        )
        assert set(templates) == {"resourceTemplates"}
        for item in templates["resourceTemplates"]:
            assert set(item) == {"uriTemplate", "name", "description", "mimeType"}
        assert calls == []

        async def read(uri):
            result = await exchange(
                "resources/read", {"uri": uri}, "ReadResourceRequest", "ReadResourceResult"
            )
            assert set(result) == {"contents"}
            assert len(result["contents"]) == 1
            block = result["contents"][0]
            assert set(block) == {"uri", "mimeType", "text"}
            assert block["uri"] == uri
            assert block["mimeType"] == "application/json"
            assert isinstance(block["text"], str)
            return json.loads(block["text"])

        manifest = await read(PREFIX + "manifest.json")
        assert set(manifest["methodResources"]) == {"jobs/start", "tasks/update"}
        assert manifest["capabilities"] == {}
        assert calls == []
        job = await read(PREFIX + "jobs/start")
        assert job["resultType"] == "task"
        assert job["ttlMs"] == 1000
        params = {"taskId": "a", "inputResponses": {"question": {"action": "accept"}}}
        uri = PREFIX + "tasks/update?params=" + quote(json.dumps(params), safe="")
        assert (await read(uri))["resultType"] == "complete"
        assert calls == ["start", params["inputResponses"]]
        for method in ("server/discover", "jobs/start", "tasks/update"):
            await exchange(method, {})
        assert calls == ["start", params["inputResponses"]]
    return captured


@pytest.mark.parametrize("version", VERSIONS)
@pytest.mark.parametrize("transport", [connect, over_http])
async def test_extension_fallback_does_not_change_legacy_wire(transport, version):
    await inspect_legacy_wire(transport, version)
