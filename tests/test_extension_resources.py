"""Method resources work without native extension support."""

import json
from urllib.parse import quote

import pytest
from pydantic import BaseModel, ConfigDict

from aiohttp_tiny_mcp import ClientError, Exchange, Extension, Registry, TextResourceContents
from aiohttp_tiny_mcp.models import ListParams, ReadResourceParams
from aiohttp_tiny_mcp.testing import connect, over_http

VERSIONS = ["2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25"]
PREFIX = "mcp-extensions://example.org/live/"


class File(BaseModel):
    name: str


class Lookup(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: int


def request_uri(method, params):
    return PREFIX + method + "?params=" + quote(json.dumps(params), safe="")


@pytest.mark.parametrize("version", VERSIONS)
@pytest.mark.parametrize("transport", [connect, over_http])
async def test_dynamic_methods_templates_dependencies_and_revision_visibility(version, transport):
    state = {"alpha": "first"}
    released = []
    registry = Registry("live", "1", page_size=1)
    extension = Extension("example.org/live")

    async def provide(ex):
        try:
            yield state
        finally:
            released.append(True)

    registry.provide(dict, provide)

    @extension.resource("skill://{name}/SKILL.md")
    async def read(args: File, current: dict):
        return TextResourceContents(uri=f"skill://{args.name}/SKILL.md", text=current[args.name])

    @extension.method("skills/list")
    async def listing(args: ListParams, current: dict, ex: Exchange):
        assert ex.adapter.version in [*VERSIONS, "2026-07-28"]
        names = sorted(current)
        start = names.index(args.cursor) + 1 if args.cursor else 0
        selected = names[start : start + 1]
        result = {"skills": [{"uri": f"skill://{name}/SKILL.md"} for name in selected]}
        if start + 1 < len(names):
            result["nextCursor"] = selected[-1]
        return result

    @extension.method("skills/get")
    async def get(args: ReadResourceParams):
        return {"skill": {"uri": args.uri}}

    @extension.method("tasks/cancel")
    async def cancel(args: ReadResourceParams):
        state.pop(args.uri)
        return {"cancelled": args.uri}

    registry.extension(extension)
    # Routes retain a snapshot even when the extension declaration changes.
    extension.methods.clear()
    extension.resources.clear()
    async with transport(registry, adapter=version) as client:
        discovery = await client.initialize()
        assert "extensions" not in discovery["capabilities"]
        assert {r.uri for r in await client.list_resources()} == {
            PREFIX + "manifest.json",
            PREFIX + "skills/list",
        }
        templates = await client.request_method("resources/templates/list")
        assert templates["resourceTemplates"][0]["uriTemplate"].startswith(PREFIX)
        manifest = json.loads(
            (await client.read_resource(PREFIX + "manifest.json")).contents[0].text
        )
        assert set(manifest["methodResources"]) == {"skills/get", "skills/list", "tasks/cancel"}
        assert manifest["methodResources"]["skills/get"]["inputSchema"]["required"] == ["uri"]
        first = json.loads((await client.read_resource(PREFIX + "skills/list")).contents[0].text)
        assert first == {"skills": [{"uri": PREFIX + "alpha/SKILL.md"}]}
        state["beta"] = "added while running"
        second = json.loads((await client.read_resource(PREFIX + "skills/list")).contents[0].text)
        assert second["nextCursor"] == "alpha"
        next_uri = request_uri("skills/list", {"cursor": second["nextCursor"]})
        last = json.loads((await client.read_resource(next_uri)).contents[0].text)
        assert last["skills"] == [{"uri": PREFIX + "beta/SKILL.md"}]
        skill_uri = last["skills"][0]["uri"]
        detail = await client.read_resource(request_uri("skills/get", {"uri": skill_uri}))
        assert json.loads(detail.contents[0].text) == {"skill": {"uri": skill_uri}}
        file = (await client.read_resource(skill_uri)).contents[0]
        assert file.uri == skill_uri
        assert file.text == "added while running"
        assert len(released) == 4
        cancelled = await client.read_resource(request_uri("tasks/cancel", {"uri": "beta"}))
        assert json.loads(cancelled.contents[0].text) == {"cancelled": "beta"}
        assert "beta" not in state
        for uri in (
            "skill://alpha/SKILL.md",
            PREFIX + "tasks/cancel",
            request_uri("tasks/cancel", {}),
        ):
            with pytest.raises(ClientError):
                await client.read_resource(uri)
        with pytest.raises(ClientError) as missing:
            await client.request_method("skills/list")
        assert missing.value.code == -32601

    async with transport(registry, adapter="2026-07-28") as client:
        await client.initialize()
        assert await client.list_resources() == []
        templates = await client.request_method("resources/templates/list")
        assert [t["uriTemplate"] for t in templates["resourceTemplates"]] == [
            "skill://{name}/SKILL.md"
        ]
        assert (await client.request_method("skills/list"))["skills"][0][
            "uri"
        ] == "skill://alpha/SKILL.md"
        for uri in (
            PREFIX + "alpha/SKILL.md",
            PREFIX + "skills/list",
            request_uri("skills/get", {"uri": "x"}),
        ):
            with pytest.raises(ClientError):
                await client.read_resource(uri)


async def test_method_resource_validation_preserves_typed_arguments():
    extension = Extension("example.org/live")

    @extension.resource("data://{name}")
    async def broad_file_template(args: File):
        return {"wrongRoute": args.name}

    @extension.method("lookup")
    async def lookup(args: Lookup):
        return {"doubled": args.value * 2}

    registry = Registry("validation", "1")
    registry.extension(extension)
    async with connect(registry, adapter="2025-11-25") as client:
        result = await client.read_resource(request_uri("lookup", {"value": 7}))
        assert json.loads(result.contents[0].text) == {"doubled": 14}
        for params in ("[]", "null", "{bad", '{"value":"bad"}', '{"value":7,"extra":true}'):
            with pytest.raises(ClientError) as error:
                await client.read_resource(PREFIX + "lookup?params=" + quote(params, safe=""))
            assert error.value.code == -32602


def test_method_route_conflicts_and_template_variables():
    extension = Extension("example.org/live")

    async def read(args: ListParams):
        return {}

    extension.method("skills/list", read)
    extension.resource("data://skills/list", read)
    registry = Registry("conflict", "1")
    with pytest.raises(ValueError, match="duplicate resource"):
        registry.extension(extension)
    assert not registry.extensions
    assert not registry.resources_fixed
    assert not registry.resources_templated
    with pytest.raises(ValueError, match="same template variables"):
        extension.resource("data://{name}", read, legacy_path="{other}")


class Job(BaseModel):
    taskId: str
    inputResponses: dict = {}


@pytest.mark.parametrize("version", [*VERSIONS, "2026-07-28"])
@pytest.mark.parametrize("transport", [connect, over_http])
async def test_complete_method_interface_including_state_changes_and_custom_results(
    version, transport
):
    from aiohttp_tiny_mcp.core import Failure, FailureKind, Rejected

    extension = Extension("example.org/live")
    state = {}

    @extension.method("jobs/start")
    async def start(args: Job):
        state[args.taskId] = {"status": "working"}
        return {"resultType": "task", "task": {"taskId": args.taskId, "status": "working"}}

    @extension.method("tasks/get")
    async def get(args: Job):
        if args.taskId not in state:
            raise Rejected(
                Failure(FailureKind.INVALID_PARAMS, "Unknown job", {"taskId": args.taskId})
            )
        return {"task": state[args.taskId]}

    @extension.method("tasks/update")
    async def update(args: Job):
        state[args.taskId]["answers"] = args.inputResponses
        return {}

    @extension.method("tasks/cancel")
    async def cancel(args: Job):
        state[args.taskId]["status"] = "cancelled"
        return {}

    registry = Registry("jobs", "1")
    registry.extension(extension)
    async with transport(registry, adapter=version) as client:
        discovery = await client.initialize()
        assert ("extensions" in discovery["capabilities"]) == (version == "2026-07-28")

        async def invoke(method, params):
            if version == "2026-07-28":
                return await client.request_method(method, params)
            resource = await client.read_resource(request_uri(method, params))
            return json.loads(resource.contents[0].text)

        result = await invoke("jobs/start", {"taskId": "a"})
        assert result["resultType"] == "task"
        assert result["task"]["status"] == "working"
        answers = {"question": {"action": "accept", "content": {"note": "✓ /?&%"}}}
        await invoke("tasks/update", {"taskId": "a", "inputResponses": answers})
        assert (await invoke("tasks/get", {"taskId": "a"}))["task"]["answers"] == answers
        await invoke("tasks/cancel", {"taskId": "a"})
        assert (await invoke("tasks/get", {"taskId": "a"}))["task"]["status"] == "cancelled"
        with pytest.raises(ClientError) as error:
            await invoke("tasks/get", {"taskId": "missing"})
        assert error.value.code == -32602
        assert str(error.value) == "Unknown job"
        assert error.value.data == {"taskId": "missing"}
