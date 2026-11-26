"""Making a container, with the flags named rather than written out.

`docker run` takes its options as a command line. A model writing one gets
the quoting wrong, so every option is a field here and `docker.configuration`
assembles what the daemon actually takes.

The default policy does not ask before this. Creating something is undone by
removing it, and a question about the most reversible action is the one that
teaches people to stop reading questions.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any

from aiodocker import Docker
from aiohttp_tiny_mcp import Exchange

from docker_mcp import daemon
from docker_mcp.consent import Policy, confirm, refusal
from docker_mcp.errors import Conflict, clear
from docker_mcp.events import publish
from docker_mcp.models import (
    Created,
    CreateRequest,
    FixedNetworkCreateRequest,
    PullRequest,
    UnprivilegedCreateRequest,
)
from docker_mcp.safety import check_container, configure_unprivileged

from .pull import pull
from .results import settled


def port_binding(spec: str) -> tuple[str, str, dict[str, str]]:
    """One published port, as `docker run -p` states it.

    Returns the container port with its protocol, and the host address to bind
    it to. Anything this cannot read is refused by name, because a port
    silently not published is found much later.
    """
    body, _, protocol = spec.partition("/")
    parts = body.split(":")
    if len(parts) == 2:
        host_ip, host_port, container_port = "", parts[0], parts[1]
    elif len(parts) == 3:
        host_ip, host_port, container_port = parts
    else:
        raise ValueError(f'cannot read port {spec!r}: write it as "8080:80" or "127.0.0.1:8080:80"')
    if not host_port.isdigit() or not container_port.isdigit():
        raise ValueError(f"cannot read port {spec!r}: both sides must be numbers")
    key = f"{container_port}/{protocol or 'tcp'}"
    return key, container_port, {"HostIp": host_ip, "HostPort": host_port}


def configuration(
    *,
    image: str,
    command: Sequence[str] | None,
    environment: Mapping[str, str],
    ports: Sequence[str],
    volumes: Sequence[str],
    network: str | None,
    restart: str,
    workdir: str | None,
    user: str | None,
) -> dict[str, Any]:
    """What `containers/create` takes, built from named arguments.

    The daemon's shape puts half of this under `HostConfig` and half beside
    it, and a port has to appear in both. Assembling it here keeps that out of
    the tool.
    """
    exposed: dict[str, Any] = {}
    bindings: dict[str, list[dict[str, str]]] = {}
    for spec in ports:
        key, _, binding = port_binding(spec)
        exposed[key] = {}
        bindings.setdefault(key, []).append(binding)
    host: dict[str, Any] = {"RestartPolicy": {"Name": restart}}
    if bindings:
        host["PortBindings"] = bindings
    if volumes:
        host["Binds"] = list(volumes)
    if network:
        host["NetworkMode"] = network
    config: dict[str, Any] = {"Image": image, "HostConfig": host}
    if command:
        config["Cmd"] = list(command)
    if environment:
        config["Env"] = [f"{key}={value}" for key, value in environment.items()]
    if exposed:
        config["ExposedPorts"] = exposed
    if workdir:
        config["WorkingDir"] = workdir
    if user:
        config["User"] = user
    return config


def creation(args: CreateRequest) -> str:
    """The question `create` asks, naming what reaches the host."""
    said = f"Create a container from {args.image}"
    if args.ports:
        said += f", publishing {', '.join(args.ports)}"
    if args.volumes:
        said += f", mounting {', '.join(args.volumes)}"
    return said + "?"


async def create(args: CreateRequest, client: Docker, ex: Exchange, policy: Policy) -> Created:
    """Create a container, and start it unless told not to.

    This is `docker run`, with the flags named rather than written into one
    string. Ports are "8080:80", volumes are "/on/host:/in/container". The
    image is downloaded first where it is missing.

    Asks first only where the policy says to: creating something is undone
    by removing it, so the default does not interrupt for this.
    """
    return await create_container(args, client, ex, policy)


async def create_container(
    args: CreateRequest, client: Docker, ex: Exchange, policy: Policy, *, unprivileged: bool = False
) -> Created:
    """Create a container after checking deployment restrictions."""
    if not await confirm(ex, policy, "create", args.image, creation(args)):
        raise Conflict(f"not created: {refusal(ex)}")

    config = configuration(
        image=args.image,
        command=args.command,
        environment=args.environment,
        ports=args.ports,
        volumes=args.volumes,
        network=args.network,
        restart=args.restart,
        workdir=args.workdir,
        user=args.user,
    )
    if unprivileged:
        await configure_unprivileged(client, args, config)

    held = {tag for raw in await daemon.images(client) for tag in raw.get("RepoTags") or []}
    if args.pull == "always" or (args.pull == "missing" and args.image not in held):
        await pull(PullRequest(image=args.image), client, ex)

    async with clear("creating the container", subject=args.name or ""):
        made = await client.containers.create(config, name=args.name)
    if unprivileged:
        try:
            await check_container(client, made)
        except Exception as error:
            raise Conflict(
                f"container {made.id} was created but refused by unprivileged mode; "
                f"it was not started: {error}"
            ) from error
    note = None
    if args.start:
        await made.start()
        state = await settled(made)
        if str(state.get("Status")) != "running":
            note = (
                f"created, but it did not stay running: it is {state.get('Status')}. "
                "Read `logs` for why."
            )
    raw = await made.show()
    await publish(ex.registry, "docker://containers")
    return Created(
        id=str(raw.get("Id", ""))[:12],
        name=str(raw.get("Name", "")).lstrip("/"),
        image=args.image,
        state=str((raw.get("State") or {}).get("Status", "created")),
        ports=list(args.ports),
        warnings=[str(item) for item in raw.get("Warnings") or []],
        note=note,
    )


def fixed_network_create(network: str) -> Callable[..., Awaitable[Created]]:
    """Bind operator settings without exposing them as tool arguments."""

    async def create_fixed_network(
        args: FixedNetworkCreateRequest, client: Docker, ex: Exchange, policy: Policy
    ) -> Created:
        """Create a container on the server's fixed network; callers cannot change the network."""
        request = CreateRequest(**args.model_dump(), network=network)
        return await create_container(request, client, ex, policy)

    return create_fixed_network


def unprivileged_create(network: str | None) -> Callable[..., Awaitable[Created]]:
    """Bind the operator's network for unprivileged container creation."""

    async def create_unprivileged(
        args: UnprivilegedCreateRequest, client: Docker, ex: Exchange, policy: Policy
    ) -> Created:
        """Create an isolated container with optional persistent named volumes.

        Volumes use name:/path[:ro|rw]. Host paths and network overrides are forbidden.
        Missing volumes use the local driver without mount options.
        """
        request = CreateRequest(**args.model_dump(), network=network)
        return await create_container(request, client, ex, policy, unprivileged=True)

    return create_unprivileged
