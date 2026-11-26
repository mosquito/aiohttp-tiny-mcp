"""Restricted Docker operations through the MCP transport and deployment parser."""

from __future__ import annotations

import pytest
from aiodocker.exceptions import DockerError
from aiohttp_tiny_mcp import Client, ClientError, Registry
from aiohttp_tiny_mcp.protocol.selection import AdapterSet
from fake_docker import CACHE

from docker_mcp.__main__ import Parser, assemble
from docker_mcp.consent import Policy
from docker_mcp.server import build

EVERY_REVISION = AdapterSet.default().adapters
MODERN = AdapterSet.default().by_version["2026-07-28"]


@pytest.fixture
def settings(request):
    return getattr(request, "param", {"unprivileged": True})


@pytest.fixture
def registry(client, fake, settings):
    fake.inspect_overrides[CACHE] = {"Mounts": [], "HostConfig": {"NetworkMode": "bridge"}}
    return build(Registry("docker", "0.1.0"), client, Policy("never"), **settings)


@pytest.mark.parametrize("adapter", EVERY_REVISION, ids=lambda adapter: adapter.version)
async def test_restricted_schema_and_unknown_arguments(url, fake, adapter):
    async with Client(url, adapter) as client:
        await client.initialize()
        offered = {tool.name: tool for tool in await client.list_tools()}
        schema = offered["create"].input_schema
        assert "network" not in schema["properties"]
        assert schema["additionalProperties"] is False
        assert "Host paths are forbidden" in schema["properties"]["volumes"]["description"]
        for argument in ("network", "privileged", "devices", "cap_add", "security_opt"):
            result = await client.call_tool("create", {"image": "redis:7", argument: "host"})
            assert result.is_error
    assert not fake.created
    assert not fake.pulled
    assert not fake.created_volumes


@pytest.mark.parametrize(
    "mount",
    [
        "/:/host",
        "/var/run/docker.sock:/var/run/docker.sock",
        "./data:/data",
        "../data:/data",
        "~/data:/data",
        "C:\\data:/data",
        "data:relative",
        "data:/data:ro,rshared",
        "data:/data:z",
        "data:/data:ro:rw",
        "data:/data\x00",
        "data:/data\n",
    ],
)
async def test_host_mounts_and_mount_flags_are_rejected(url, fake, mount):
    async with Client(url, MODERN) as client:
        await client.initialize()
        result = await client.call_tool("create", {"image": "redis:7", "volumes": [mount]})
        assert result.is_error
    assert not fake.created
    assert not fake.pulled
    assert not fake.created_volumes


async def test_named_volume_survives_container_replacement(url, fake):
    async with Client(url, MODERN) as client:
        await client.initialize()
        first = await client.call_tool(
            "create", {"image": "redis:7", "name": "first", "volumes": ["persistent:/data"]}
        )
        assert not first.is_error
        written = await client.call_tool(
            "write", {"container": "first", "path": "/data/note.txt", "content": "persistent data"}
        )
        assert not written.is_error
        removed = await client.call_tool("remove", {"container": "first", "force": True})
        assert not removed.is_error
        second = await client.call_tool(
            "create", {"image": "redis:7", "name": "second", "volumes": ["persistent:/data:ro"]}
        )
        assert not second.is_error
        read = await client.call_tool("read", {"container": "second", "path": "/data/note.txt"})
        assert read.structured_content["text"] == "persistent data"
    assert fake.created_volumes == [{"Name": "persistent", "Driver": "local"}]
    assert any(volume["Name"] == "persistent" for volume in fake.volumes)
    host = fake.created[-1]["config"]["HostConfig"]
    assert host["Mounts"] == [
        {"Type": "volume", "Source": "persistent", "Target": "/data", "ReadOnly": True}
    ]
    assert "Binds" not in host
    assert host["Privileged"] is False
    assert host["SecurityOpt"] == ["no-new-privileges:true"]
    assert host["NetworkMode"] == "bridge"


@pytest.mark.parametrize(
    "volume",
    [
        {
            "Name": "unsafe",
            "Driver": "local",
            "Options": {"type": "none", "o": "bind", "device": "/"},
        },
        {"Name": "unsafe", "Driver": "external-plugin", "Options": {}},
    ],
)
async def test_existing_unsafe_volume_is_rejected(url, fake, volume):
    fake.volumes.append(volume)
    async with Client(url, MODERN) as client:
        await client.initialize()
        result = await client.call_tool("create", {"image": "redis:7", "volumes": ["unsafe:/data"]})
    assert result.is_error
    assert "local volume without options" in result.content[0].text
    assert not fake.created
    assert not fake.created_volumes
    assert not fake.pulled


async def test_volume_name_collision_is_checked_after_creation(url, fake, client, monkeypatch):
    fake.volumes.append({"Name": "collision", "Driver": "local", "Options": {"device": "/"}})

    async def missing(name):
        raise DockerError(404, "The volume was absent during the first lookup")

    monkeypatch.setattr(client.volumes, "get", missing)
    async with Client(url, MODERN) as mcp:
        await mcp.initialize()
        result = await mcp.call_tool("create", {"image": "redis:7", "volumes": ["collision:/data"]})
    assert result.is_error
    assert "local volume without options" in result.content[0].text
    assert fake.created_volumes == [{"Name": "collision", "Driver": "local"}]
    assert not fake.created
    assert not fake.pulled


@pytest.mark.parametrize("status", [403, 500])
async def test_volume_inspection_failure_does_not_create_a_volume(
    url, fake, client, monkeypatch, status
):
    async def failed(name):
        raise DockerError(status, "Volume inspection failed")

    monkeypatch.setattr(client.volumes, "get", failed)
    async with Client(url, MODERN) as mcp:
        await mcp.initialize()
        result = await mcp.call_tool("create", {"image": "redis:7", "volumes": ["data:/data"]})
    assert result.is_error
    assert not fake.created_volumes
    assert not fake.created
    assert not fake.pulled


DANGEROUS_CONFIGURATIONS = [
    {"HostConfig": {"Privileged": True}},
    {"HostConfig": {"CapAdd": ["SYS_ADMIN"]}},
    {"HostConfig": {"Devices": [{"PathOnHost": "/dev/sda"}]}},
    {"HostConfig": {"DeviceRequests": [{"Driver": "nvidia"}]}},
    {"HostConfig": {"DeviceCgroupRules": ["a *:* rwm"]}},
    {"HostConfig": {"VolumesFrom": ["host-access"]}},
    {"HostConfig": {"Binds": ["/:/host:ro"]}},
    {"HostConfig": {"Mounts": [{"Type": "bind", "Source": "/", "Target": "/host"}]}},
    {"HostConfig": {"NetworkMode": "host"}},
    {"HostConfig": {"NetworkMode": "container:other"}},
    {"HostConfig": {"PidMode": "host"}},
    {"HostConfig": {"IpcMode": "host"}},
    {"HostConfig": {"UTSMode": "host"}},
    {"HostConfig": {"UsernsMode": "host"}},
    {"HostConfig": {"CgroupnsMode": "host"}},
    {"HostConfig": {"Runtime": "custom-runtime"}},
    {"HostConfig": {"SecurityOpt": ["seccomp=unconfined"]}},
    {"HostConfig": {"MaskedPaths": []}},
    {"HostConfig": {"MaskedPaths": ["/proc/kcore"]}},
    {"HostConfig": {"ReadonlyPaths": []}},
    {"HostConfig": {"ReadonlyPaths": ["/proc/sys"]}},
    {"Mounts": [{"Type": "bind", "Source": "/", "Destination": "/host", "RW": False}]},
    {"Mounts": [{"Type": "unknown", "Destination": "/data"}]},
    {"Mounts": [{"Type": "volume", "Name": "unsafe", "Destination": "/data"}]},
]


@pytest.mark.parametrize("configuration", DANGEROUS_CONFIGURATIONS)
async def test_existing_unsafe_container_cannot_execute(url, fake, configuration):
    fake.inspect_overrides[CACHE].update(configuration)
    fake.volumes.append({"Name": "unsafe", "Driver": "local", "Options": {"device": "/"}})
    async with Client(url, MODERN) as client:
        await client.initialize()
        result = await client.call_tool(
            "exec", {"container": "cache", "command": ["cat", "/etc/passwd"]}
        )
    assert result.is_error
    assert not fake.executed


@pytest.mark.parametrize(
    "tool,arguments",
    [
        ("read", {"path": "/etc/redis.conf"}),
        ("write", {"path": "/etc/redis.conf", "content": "overwrite"}),
        ("exec", {"command": ["ls"]}),
        ("start", {}),
        ("restart", {}),
    ],
)
async def test_host_access_is_blocked_before_container_action(url, fake, tool, arguments):
    fake.inspect_overrides[CACHE]["HostConfig"] = {"Privileged": True}
    async with Client(url, MODERN) as client:
        await client.initialize()
        result = await client.call_tool(tool, {"container": "cache", **arguments})
    assert result.is_error
    assert "unprivileged mode" in result.content[0].text
    assert not fake.archive_reads
    assert not fake.written
    assert not fake.executed
    assert not fake.started
    assert not fake.restarted


async def test_safe_existing_container_allows_file_access_and_commands(url, fake):
    async with Client(url, MODERN) as client:
        await client.initialize()
        for tool, arguments in (
            ("read", {"path": "/etc/redis.conf"}),
            ("write", {"path": "/tmp/note", "content": "safe"}),
            ("exec", {"command": ["ls"]}),
            ("start", {}),
            ("restart", {}),
        ):
            result = await client.call_tool(tool, {"container": "cache", **arguments})
            assert not result.is_error, (tool, result)
    assert fake.executed
    assert fake.written
    assert fake.started
    assert fake.restarted


@pytest.mark.parametrize(
    "settings",
    [{"network": "shop_default"}, {"network": "shop_default", "unprivileged": True}],
    indirect=True,
)
@pytest.mark.parametrize("adapter", EVERY_REVISION, ids=lambda adapter: adapter.version)
async def test_fixed_network_hides_and_rejects_network_management(url, fake, adapter):
    async with Client(url, adapter) as client:
        await client.initialize()
        offered = {tool.name: tool for tool in await client.list_tools()}
        assert "networks" not in offered
        assert "network" not in offered["create"].input_schema["properties"]
        assert "networks" not in offered["prune"].input_schema["properties"]["what"]["enum"]
        refused = await client.call_tool("create", {"image": "redis:7", "network": "host"})
        assert refused.is_error
        refused = await client.call_tool("prune", {"what": "networks"})
        assert refused.is_error
        with pytest.raises(ClientError):
            await client.call_tool("networks", {})
        result = await client.call_tool("create", {"image": "redis:7", "start": False})
        assert not result.is_error
    assert fake.created[-1]["config"]["HostConfig"]["NetworkMode"] == "shop_default"
    assert not fake.pruned


@pytest.mark.parametrize(
    "settings",
    [
        {"unprivileged": True, "network": "host"},
        {"unprivileged": True, "network": "container:cache"},
    ],
    indirect=True,
)
async def test_unsafe_deployment_network_is_rejected(url, fake):
    async with Client(url, MODERN) as client:
        await client.initialize()
        result = await client.call_tool("create", {"image": "redis:7"})
    assert result.is_error
    assert not fake.created
    assert not fake.pulled


@pytest.mark.parametrize(
    "settings", [{"unprivileged": True, "network": "shop_default"}], indirect=True
)
async def test_non_bridge_deployment_network_is_rejected(url, fake):
    fake.networks = [{**network, "Driver": "macvlan"} for network in fake.networks]
    async with Client(url, MODERN) as client:
        await client.initialize()
        result = await client.call_tool("create", {"image": "redis:7"})
    assert result.is_error
    assert not fake.created


@pytest.mark.parametrize("settings", [{"unprivileged": True, "network": "none"}], indirect=True)
async def test_network_none_is_supported(url, fake):
    async with Client(url, MODERN) as client:
        await client.initialize()
        result = await client.call_tool("create", {"image": "redis:7"})
    assert not result.is_error
    assert fake.created[-1]["config"]["HostConfig"]["NetworkMode"] == "none"


@pytest.mark.parametrize("settings", [{}], indirect=True)
async def test_unrestricted_mode_keeps_existing_options(url, fake):
    async with Client(url, MODERN) as client:
        await client.initialize()
        offered = {tool.name: tool for tool in await client.list_tools()}
        assert "networks" in offered
        assert "network" in offered["create"].input_schema["properties"]
        result = await client.call_tool(
            "create",
            {"image": "redis:7", "network": "host", "volumes": ["/tmp:/data"], "start": False},
        )
        assert not result.is_error
    host = fake.created[-1]["config"]["HostConfig"]
    assert host["Binds"] == ["/tmp:/data"]
    assert host["NetworkMode"] == "host"


async def test_registry_restrictions_are_instance_local(client):
    restricted = build(Registry("restricted", "0.1.0"), client, unprivileged=True, network="bridge")
    normal = build(Registry("normal", "0.1.0"), client)
    assert "network" not in restricted.tools["create"].input_schema["properties"]
    assert "network" in normal.tools["create"].input_schema["properties"]
    assert "networks" not in restricted.tools
    assert "networks" in normal.tools


@pytest.mark.parametrize("source", ["cli", "environment", "config"])
@pytest.mark.parametrize("unprivileged", [False, True])
def test_parser_accepts_deployment_restrictions(source, unprivileged, monkeypatch, tmp_path):
    arguments = []
    config = tmp_path / "docker-mcp.ini"
    monkeypatch.delenv("DOCKER_MCP_UNPRIVILEGED", raising=False)
    monkeypatch.delenv("DOCKER_MCP_NETWORK", raising=False)
    if source == "cli":
        arguments = ["--network", "shop_default"]
        if unprivileged:
            arguments.append("--unprivileged")
    elif source == "environment":
        if unprivileged:
            monkeypatch.setenv("DOCKER_MCP_UNPRIVILEGED", "true")
        monkeypatch.setenv("DOCKER_MCP_NETWORK", "shop_default")
    else:
        content = "[DEFAULT]\nnetwork=shop_default\n"
        if unprivileged:
            content += "unprivileged=true\n"
        config.write_text(content)
    parser = Parser(auto_env_var_prefix="DOCKER_MCP_", config_files=[str(config)])
    parser.parse_args(arguments)
    assert parser.unprivileged is unprivileged
    assert parser.network == "shop_default"


@pytest.mark.parametrize("unprivileged", [False, True])
async def test_assemble_passes_restrictions_to_registry(tmp_path, docker_url, unprivileged):
    parser = Parser()
    parser.parse_args(
        [
            "--network",
            "shop_default",
            "--docker-url",
            docker_url,
            "--state",
            str(tmp_path / "assembled.sqlite3"),
        ]
        + (["--unprivileged"] if unprivileged else [])
    )
    registry, storage, client = assemble(parser)
    try:
        assert "network" not in registry.tools["create"].input_schema["properties"]
        assert "networks" not in registry.tools
        assert (
            "Host paths are forbidden"
            in registry.tools["create"].input_schema["properties"]["volumes"]["description"]
        ) is unprivileged
    finally:
        await client.close()
        await storage.close()
