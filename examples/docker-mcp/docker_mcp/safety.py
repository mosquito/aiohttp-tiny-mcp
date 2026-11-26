"""Deployment restrictions for Docker host access."""

from __future__ import annotations

from typing import Any

from aiodocker import Docker
from aiodocker.containers import DockerContainer
from aiodocker.exceptions import DockerError

from . import daemon
from .errors import Refused, clear
from .models import CreateRequest, UnprivilegedCreateRequest

PROTECTED_PATHS = {
    "MaskedPaths": frozenset(
        (
            "/proc/asound",
            "/proc/acpi",
            "/proc/kcore",
            "/proc/keys",
            "/proc/latency_stats",
            "/proc/timer_list",
            "/proc/timer_stats",
            "/proc/sched_debug",
            "/proc/scsi",
            "/sys/firmware",
            "/sys/devices/virtual/powercap",
        )
    ),
    "ReadonlyPaths": frozenset(
        (
            "/proc/bus",
            "/proc/fs",
            "/proc/irq",
            "/proc/sys",
            "/proc/sysrq-trigger",
        )
    ),
}


async def check_volume(client: Docker, name: str, *, create: bool = False) -> None:
    """Allow local volumes without mount options, including existing names."""
    async with clear("checking the volume", subject=name):
        try:
            volume = await client.volumes.get(name)
        except DockerError as error:
            if error.status != 404 or not create:
                raise
            volume = await client.volumes.create({"Name": name, "Driver": "local"})
        raw = await volume.show()
    if raw.get("Driver") != "local" or raw.get("Options"):
        raise Refused(f"unprivileged mode requires a local volume without options: {name}")


async def check_network(client: Docker, name: str) -> None:
    """Allow bridge networks and the built-in network without connectivity."""
    if name == "none":
        return
    if name == "host" or name.startswith("container:"):
        raise Refused("unprivileged mode forbids host and container network namespaces")
    async with clear("checking the network", subject=name):
        network = await client.networks.get(name)
        raw = await network.show()
    if raw.get("Driver") != "bridge" or raw.get("Scope") != "local":
        raise Refused(f"unprivileged mode requires a local bridge network: {name}")


async def configure_unprivileged(
    client: Docker, args: CreateRequest, config: dict[str, Any]
) -> None:
    """Apply deployment settings before the container is created."""
    host = config["HostConfig"]
    checked = UnprivilegedCreateRequest.model_validate(args.model_dump(exclude={"network"}))
    network = args.network or "bridge"
    await check_network(client, network)
    mounts = []
    for spec in checked.volumes:
        source, target, *mode = spec.split(":")
        await check_volume(client, source, create=True)
        mounts.append(
            {"Type": "volume", "Source": source, "Target": target, "ReadOnly": mode == ["ro"]}
        )
    host.pop("Binds", None)
    if mounts:
        host["Mounts"] = mounts
    host["NetworkMode"] = network
    host["CgroupnsMode"] = "private"
    host["IpcMode"] = "private"
    host["SecurityOpt"] = ["no-new-privileges:true"]
    host["Privileged"] = False


async def check_container(client: Docker, container: DockerContainer) -> None:
    """Check existing containers before file access or process execution."""
    async with clear("checking container isolation"):
        raw = await container.show()
    host = raw.get("HostConfig")
    if not isinstance(host, dict):
        raise Refused("unprivileged mode requires container isolation details")
    for key in (
        "Privileged",
        "CapAdd",
        "Devices",
        "DeviceRequests",
        "DeviceCgroupRules",
        "VolumesFrom",
        "ExtraHosts",
        "Sysctls",
        "StorageOpt",
        "UseApiSocket",
    ):
        if host.get(key):
            raise Refused(f"unprivileged mode forbids containers with {key}")
    for key, allowed in (
        ("PidMode", ("", "private")),
        ("IpcMode", ("", "private", "none")),
        ("UTSMode", ("", "private")),
        ("UsernsMode", ("", "private")),
        ("CgroupnsMode", ("", "private")),
        ("Runtime", ("", "runc")),
    ):
        if (host.get(key) or "") not in allowed:
            raise Refused(f"unprivileged mode forbids this {key}")
    for option in host.get("SecurityOpt") or []:
        if option not in (
            "no-new-privileges",
            "no-new-privileges:true",
            "no-new-privileges=true",
        ):
            raise Refused("unprivileged mode forbids custom security options")
    for key, required in PROTECTED_PATHS.items():
        if key in host and not required.issubset(host[key] or []):
            raise Refused(f"unprivileged mode requires protected system paths in {key}")
    for spec in host.get("Binds") or []:
        UnprivilegedCreateRequest(image="unused", volumes=[spec])
    for mount in host.get("Mounts") or []:
        if mount.get("Type") not in ("volume", "tmpfs") or mount.get("VolumeOptions"):
            raise Refused("unprivileged mode forbids host mounts and volume options")
    for mount in raw.get("Mounts") or []:
        if mount.get("Type") == "volume" and mount.get("Name"):
            await check_volume(client, mount["Name"])
        elif mount.get("Type") != "tmpfs":
            raise Refused("unprivileged mode forbids host mounts")
    network = host.get("NetworkMode") or "bridge"
    await check_network(client, "bridge" if network == "default" else network)
    for name in (raw.get("NetworkSettings") or {}).get("Networks") or {}:
        await check_network(client, name)


async def checked_container(client: Docker, reference: str) -> str:
    """Resolve and check a container, then return its immutable ID."""
    container = await daemon.find(client, reference)
    await check_container(client, container)
    return container.id
