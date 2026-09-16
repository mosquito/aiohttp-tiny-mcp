"""HTTP Docker test double with framed streams, tar archives, and bounded or live events."""

from __future__ import annotations

import asyncio
import io
import json
import tarfile
import time
from datetime import datetime, timezone
from typing import Any

from aiohttp import web

HELD: web.AppKey[Any] = web.AppKey("events_held", object)

VERSION = {
    "Version": "27.1.0",
    "ApiVersion": "1.46",
    "Os": "linux",
    "Arch": "arm64",
}

CACHE = "1111111111112222222222"
WORKER = "3333333333334444444444"


def frame(payload: bytes, stream: int = 1) -> bytes:
    """Encode Docker multiplexing: stream byte, three zero bytes, four-byte length, payload."""
    return bytes([stream, 0, 0, 0]) + len(payload).to_bytes(4, "big") + payload


def container(
    identity: str,
    name: str,
    image: str = "redis:7",
    state: str = "running",
    **extra: Any,
) -> dict[str, Any]:
    return {
        "Id": identity,
        "Names": [f"/{name}"],
        "Image": image,
        "ImageID": f"sha256:{image}",
        "State": state,
        "Status": "Up 3 hours" if state == "running" else "Exited (1) 2 minutes ago",
        "Ports": [{"IP": "0.0.0.0", "PublicPort": 6379, "PrivatePort": 6379, "Type": "tcp"}],
        "Mounts": [{"Type": "volume", "Name": f"{name}-data", "Destination": "/data"}],
        "NetworkSettings": {"Networks": {"bridge" if state == "running" else "shop_default": {}}},
        **extra,
    }


def inspected(raw: dict[str, Any], **extra: Any) -> dict[str, Any]:
    state = str(raw.get("State", "running"))
    return {
        "Id": raw["Id"],
        "Name": raw["Names"][0],
        "Created": "2026-09-01T10:00:00Z",
        "Path": "redis-server",
        "RestartCount": 0,
        "State": {
            "Status": state,
            "ExitCode": 0 if state == "running" else 1,
            "Error": "",
            "Health": {"Status": "healthy"} if state == "running" else None,
        },
        "Config": {
            "Image": raw.get("Image", "redis:7"),
            "Cmd": ["redis-server"],
            "Env": ["PATH=/usr/bin", "REDIS_PASSWORD=hunter2"],
            "Labels": {
                "com.docker.compose.project": "shop",
                "com.docker.compose.service": raw["Names"][0].lstrip("/"),
            },
            "Tty": False,
        },
        "HostConfig": {"RestartPolicy": {"Name": "unless-stopped"}},
        "NetworkSettings": {"Networks": {"bridge": {}}, "Ports": {}},
        "Mounts": [{"Source": "/data", "Destination": "/var/lib/redis"}],
        **extra,
    }


IMAGES = [
    {
        "Id": "sha256:aaaabbbbcccc111122223333",
        "RepoTags": ["redis:7"],
        "Size": 130_000_000,
        "Created": 1_760_000_000,
    },
    {
        "Id": "sha256:ddddeeeeffff444455556666",
        "RepoTags": ["postgres:16"],
        "Size": 420_000_000,
        "Created": 1_759_000_000,
    },
]

NETWORKS = [
    {
        "Id": "netnetnetnet0000",
        "Name": "bridge",
        "Driver": "bridge",
        "Scope": "local",
        "IPAM": {"Config": [{"Subnet": "172.17.0.0/16"}]},
        "Containers": {CACHE: {"Name": "cache"}},
    },
    {
        "Id": "netnetnetnet1111",
        "Name": "shop_default",
        "Driver": "bridge",
        "Scope": "local",
        "IPAM": {"Config": [{"Subnet": "172.20.0.0/16"}]},
        "Containers": {},
    },
]

VOLUMES = [
    {
        "Name": "cache-data",
        "Driver": "local",
        "Mountpoint": "/var/lib/docker/volumes/cache-data/_data",
        "CreatedAt": "2026-08-01T09:00:00Z",
    },
    {
        "Name": "orphan-data",
        "Driver": "local",
        "Mountpoint": "/var/lib/docker/volumes/orphan-data/_data",
        "CreatedAt": "2026-07-01T09:00:00Z",
    },
]


def stamp(seconds: int) -> str:
    """A log timestamp as the daemon writes it, to the nanosecond."""
    return f"2026-09-13T10:00:{seconds:02d}.000000000Z"


def epoch_of(written: str) -> float:
    """Accept Unix seconds for since, matching Docker's timestamp asymmetry."""
    head, _, fraction = written.rstrip("Z").partition(".")
    moment = datetime.strptime(head, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    return moment.timestamp() + float(f"0.{fraction or 0}")


class FakeDocker:
    """Holds the state the tests act on, so they can assert it changed."""

    def __init__(self) -> None:
        self.containers = {
            CACHE: container(CACHE, "cache"),
            WORKER: container(WORKER, "worker", image="python:3.12", state="exited"),
        }
        self.images = list(IMAGES)
        self.networks = list(NETWORKS)
        self.volumes = list(VOLUMES)
        self.removed: list[str] = []
        self.removed_images: list[str] = []
        self.pruned: list[str] = []
        self.started: list[str] = []
        self.stopped: list[str] = []
        self.created: list[dict[str, Any]] = []
        self.logs: dict[str, list[tuple[str, str, str]]] = {
            CACHE: [
                (stamp(1), "stdout", "ready to accept connections"),
                (stamp(2), "stdout", "saving to disk"),
            ],
            WORKER: [
                (stamp(1), "stdout", "Traceback"),
                (stamp(2), "stderr", "ValueError: no such queue"),
            ],
        }
        self.files: dict[str, dict[str, bytes]] = {
            CACHE: {"/etc/redis.conf": b"maxmemory 256mb\n"},
            WORKER: {"/app/settings.ini": b"[queue]\nname = missing\n"},
        }
        self.executed: list[dict[str, Any]] = []
        self.pulled: list[str] = []
        self.written: list[tuple[str, str, bytes]] = []
        self.exec_stdout = b"bin\nboot\netc\n"
        self.exec_stderr = b""
        self.exec_exit = 0
        self.exec_delay = 0.0
        self.events = [
            {
                "Type": "container",
                "Action": "die",
                "time": int(time.time()) - 30,
                "Actor": {"ID": WORKER, "Attributes": {"name": "worker", "exitCode": "1"}},
            },
            {
                "Type": "container",
                "Action": "start",
                "time": int(time.time()) - 20,
                "Actor": {"ID": CACHE, "Attributes": {"name": "cache"}},
            },
        ]

    def find(self, reference: str) -> str | None:
        """Resolve exact names and id prefixes only; partial-name matching belongs to the server."""
        for identity, raw in self.containers.items():
            if identity.startswith(reference) or f"/{reference}" in raw["Names"]:
                return identity
        return None

    def app(self) -> web.Application:
        app = web.Application()
        app.router.add_get("/version", self.version)
        app.router.add_get("/{api:v[0-9.]+}/version", self.version)
        app.router.add_get("/{api:v[0-9.]+}/containers/json", self.list_containers)
        app.router.add_post("/{api:v[0-9.]+}/containers/create", self.create)
        app.router.add_post("/{api:v[0-9.]+}/containers/prune", self.prune_containers)
        app.router.add_get("/{api:v[0-9.]+}/containers/{id}/json", self.inspect)
        app.router.add_get("/{api:v[0-9.]+}/containers/{id}/logs", self.container_logs)
        app.router.add_get("/{api:v[0-9.]+}/containers/{id}/stats", self.stats)
        app.router.add_get("/{api:v[0-9.]+}/containers/{id}/archive", self.get_archive)
        app.router.add_put("/{api:v[0-9.]+}/containers/{id}/archive", self.put_archive)
        app.router.add_post("/{api:v[0-9.]+}/containers/{id}/start", self.start)
        app.router.add_post("/{api:v[0-9.]+}/containers/{id}/stop", self.stop)
        app.router.add_post("/{api:v[0-9.]+}/containers/{id}/restart", self.restart)
        app.router.add_delete("/{api:v[0-9.]+}/containers/{id}", self.remove)
        app.router.add_post("/{api:v[0-9.]+}/containers/{id}/exec", self.make_exec)
        app.router.add_post("/{api:v[0-9.]+}/exec/{id}/start", self.start_exec)
        app.router.add_get("/{api:v[0-9.]+}/exec/{id}/json", self.inspect_exec)
        app.router.add_get("/{api:v[0-9.]+}/images/json", self.list_images)
        app.router.add_post("/{api:v[0-9.]+}/images/create", self.pull)
        app.router.add_post("/{api:v[0-9.]+}/images/prune", self.prune_images)
        app.router.add_delete("/{api:v[0-9.]+}/images/{name:.+}", self.remove_image)
        app.router.add_get("/{api:v[0-9.]+}/networks", self.list_networks)
        app.router.add_post("/{api:v[0-9.]+}/networks/prune", self.prune_networks)
        app.router.add_get("/{api:v[0-9.]+}/volumes", self.list_volumes)
        app.router.add_post("/{api:v[0-9.]+}/volumes/prune", self.prune_volumes)
        app.router.add_get("/{api:v[0-9.]+}/events", self.event_stream)
        return app

    async def version(self, request: web.Request) -> web.Response:
        return web.json_response(VERSION)

    async def list_containers(self, request: web.Request) -> web.Response:
        everything = request.query.get("all") in ("1", "true", "True")
        found = [raw for raw in self.containers.values() if everything or raw["State"] == "running"]
        return web.json_response(found)

    def resolve(self, request: web.Request) -> str:
        identity = self.find(request.match_info["id"])
        if identity is None:
            raise web.HTTPNotFound(
                text=json.dumps({"message": "No such container"}),
                content_type="application/json",
            )
        return identity

    async def inspect(self, request: web.Request) -> web.Response:
        identity = self.resolve(request)
        return web.json_response(inspected(self.containers[identity]))

    async def container_logs(self, request: web.Request) -> web.StreamResponse:
        """Filter one log stream by stdout/stderr, since, and tail, as Docker does."""
        identity = self.resolve(request)
        query = request.query
        wanted = {
            name
            for name, flag in (("stdout", "stdout"), ("stderr", "stderr"))
            if query.get(flag) in ("1", "true", "True")
        }
        held = self.logs.get(identity, [])
        since = query.get("since")
        if since:
            held = [item for item in held if epoch_of(item[0]) >= float(since)]
        tail = query.get("tail")
        if tail and tail.isdigit():
            held = held[-int(tail) :]
        lines = [item for item in held if item[1] in wanted]
        dated = query.get("timestamps") in ("1", "true", "True")
        response = web.StreamResponse()
        await response.prepare(request)
        for at, stream, text in lines:
            written = f"{at} {text}\n" if dated else f"{text}\n"
            await response.write(frame(written.encode(), 1 if stream == "stdout" else 2))
        await response.write_eof()
        return response

    async def stats(self, request: web.Request) -> web.StreamResponse:
        """Two samples, so a rate can be worked out from them."""
        self.resolve(request)
        response = web.StreamResponse()
        await response.prepare(request)
        for step in (0, 1):
            await response.write(json.dumps(self.sample(step)).encode() + b"\n")
        await response.write_eof()
        return response

    def sample(self, step: int) -> dict[str, Any]:
        return {
            "cpu_stats": {
                "cpu_usage": {"total_usage": 1_000_000_000 + step * 100_000_000},
                "system_cpu_usage": 10_000_000_000 + step * 1_000_000_000,
                "online_cpus": 4,
            },
            "precpu_stats": {
                "cpu_usage": {"total_usage": 1_000_000_000},
                "system_cpu_usage": 10_000_000_000,
            },
            "memory_stats": {"usage": 52_000_000, "limit": 260_000_000},
            "networks": {"eth0": {"rx_bytes": 4_000_000, "tx_bytes": 2_000_000}},
            "blkio_stats": {
                "io_service_bytes_recursive": [
                    {"op": "read", "value": 8_000_000},
                    {"op": "write", "value": 1_000_000},
                ]
            },
            "pids_stats": {"current": 7},
        }

    async def get_archive(self, request: web.Request) -> web.Response:
        identity = self.resolve(request)
        path = request.query.get("path", "")
        held = self.files.get(identity, {})
        if path not in held and any(name.startswith(path.rstrip("/") + "/") for name in held):
            return web.Response(
                body=self.directory_archive(held, path), content_type="application/x-tar"
            )
        if path not in held:
            raise web.HTTPNotFound(
                text=json.dumps(
                    {"message": f"Could not find the file {path} in container {identity}"}
                ),
                content_type="application/json",
            )
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w") as archive:
            entry = tarfile.TarInfo(name=path.rsplit("/", 1)[-1])
            entry.size = len(held[path])
            archive.addfile(entry, io.BytesIO(held[path]))
        return web.Response(body=buffer.getvalue(), content_type="application/x-tar")

    def directory_archive(self, held: dict[str, bytes], path: str) -> bytes:
        """A directory as the daemon sends it: the directory, then what is in
        it, each named below it. This is what makes a directory look like a
        file to anything that reads only the first member."""
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w") as archive:
            top = tarfile.TarInfo(name=path.rsplit("/", 1)[-1])
            top.type = tarfile.DIRTYPE
            archive.addfile(top)
            for name, content in held.items():
                if name.startswith(path.rstrip("/") + "/"):
                    entry = tarfile.TarInfo(name=f"{top.name}/{name.rsplit('/', 1)[-1]}")
                    entry.size = len(content)
                    archive.addfile(entry, io.BytesIO(content))
        return buffer.getvalue()

    async def put_archive(self, request: web.Request) -> web.Response:
        identity = self.resolve(request)
        directory = request.query.get("path", "/").rstrip("/")
        body = await request.read()
        with tarfile.open(fileobj=io.BytesIO(body), mode="r") as archive:
            for entry in archive.getmembers():
                handle = archive.extractfile(entry)
                content = handle.read() if handle is not None else b""
                path = f"{directory}/{entry.name}"
                self.files.setdefault(identity, {})[path] = content
                self.written.append((identity, path, content))
        return web.Response(status=200)

    async def create(self, request: web.Request) -> web.Response:
        config = await request.json()
        name = request.query.get("name") or "invented"
        identity = f"{len(self.created):04d}" + "abcdefabcdef"[: 22 - 4]
        self.created.append({"name": name, "config": config})
        self.containers[identity] = container(
            identity, name, image=str(config.get("Image", "")), state="created"
        )
        self.files[identity] = {}
        return web.json_response({"Id": identity, "Warnings": []})

    async def start(self, request: web.Request) -> web.Response:
        identity = self.resolve(request)
        self.started.append(identity)
        self.containers[identity]["State"] = "running"
        return web.Response(status=204)

    async def stop(self, request: web.Request) -> web.Response:
        identity = self.resolve(request)
        self.stopped.append(identity)
        self.containers[identity]["State"] = "exited"
        return web.Response(status=204)

    async def restart(self, request: web.Request) -> web.Response:
        identity = self.resolve(request)
        self.containers[identity]["State"] = "running"
        return web.Response(status=204)

    async def remove(self, request: web.Request) -> web.Response:
        identity = self.resolve(request)
        self.removed.append(identity)
        self.containers.pop(identity)
        return web.Response(status=204)

    async def make_exec(self, request: web.Request) -> web.Response:
        self.resolve(request)
        body = await request.json()
        self.executed.append(dict(body))
        return web.json_response({"Id": "exec-1"})

    async def start_exec(self, request: web.Request) -> web.StreamResponse:
        """Write the 101 UPGRADED response directly because aiohttp cannot express the socket
        takeover.
        """
        if self.exec_delay:
            await asyncio.sleep(self.exec_delay)
        transport = request.transport
        assert transport is not None
        payload = b""
        if self.exec_stdout:
            payload += frame(self.exec_stdout, 1)
        if self.exec_stderr:
            payload += frame(self.exec_stderr, 2)
        transport.write(
            b"HTTP/1.1 101 UPGRADED\r\n"
            b"Content-Type: application/vnd.docker.multiplexed-stream\r\n"
            b"Connection: Upgrade\r\n"
            b"Upgrade: tcp\r\n\r\n" + payload
        )
        transport.close()
        return web.Response()

    async def inspect_exec(self, request: web.Request) -> web.Response:
        return web.json_response(
            {"ExitCode": self.exec_exit, "Running": False, "ProcessConfig": {"tty": False}}
        )

    async def list_images(self, request: web.Request) -> web.Response:
        return web.json_response(self.images)

    async def remove_image(self, request: web.Request) -> web.Response:
        name = request.match_info["name"]
        self.removed_images.append(name)
        self.images = [
            raw for raw in self.images if name not in [*(raw.get("RepoTags") or []), raw["Id"]]
        ]
        return web.json_response([{"Untagged": name}, {"Deleted": "sha256:gone"}])

    async def pull(self, request: web.Request) -> web.StreamResponse:
        self.pulled.append(request.query.get("fromImage", ""))
        response = web.StreamResponse()
        await response.prepare(request)
        for layer in ("a1", "b2", "c3"):
            await response.write(
                json.dumps({"id": layer, "status": "Downloading"}).encode() + b"\n"
            )
            await response.write(
                json.dumps({"id": layer, "status": "Download complete"}).encode() + b"\n"
            )
        await response.write_eof()
        return response

    async def list_networks(self, request: web.Request) -> web.Response:
        """Without `Containers`, which is what the daemon does.

        Only `inspect` reports what is attached. A stand-in that answered more
        than the real endpoint would hide the code that makes up for it.
        """
        return web.json_response(
            [
                {key: value for key, value in raw.items() if key != "Containers"}
                for raw in self.networks
            ]
        )

    async def list_volumes(self, request: web.Request) -> web.Response:
        return web.json_response({"Volumes": self.volumes})

    def pruned_reply(self, what: str, key: str, names: list[str]) -> web.Response:
        self.pruned.append(what)
        return web.json_response({key: names, "SpaceReclaimed": 12_000_000})

    async def prune_containers(self, request: web.Request) -> web.Response:
        return self.pruned_reply("containers", "ContainersDeleted", [WORKER])

    async def prune_images(self, request: web.Request) -> web.Response:
        return self.pruned_reply("images", "ImagesDeleted", ["sha256:dangling"])

    async def prune_volumes(self, request: web.Request) -> web.Response:
        return self.pruned_reply("volumes", "VolumesDeleted", ["orphan-data"])

    async def prune_networks(self, request: web.Request) -> web.Response:
        return self.pruned_reply("networks", "NetworksDeleted", ["shop_default"])

    async def event_stream(self, request: web.Request) -> web.StreamResponse:
        """Return history when until is provided; otherwise keep streaming."""
        response = web.StreamResponse()
        await response.prepare(request)
        for event in self.events:
            await response.write(json.dumps(event).encode() + b"\n")
        if request.query.get("until"):
            await response.write_eof()
            return response
        await request.app[HELD].wait()
        return response
