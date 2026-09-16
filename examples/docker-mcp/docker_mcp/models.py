"""Tool argument and result models. Results report truncation and observed post-action state."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

Stream = Literal["stdout", "stderr"]


class Nothing(BaseModel):
    """No arguments."""


class ContainerFilter(BaseModel):
    all: bool = Field(
        False,
        description="Include stopped containers. False lists only running ones.",
    )
    name: str | None = Field(
        None,
        description="Keep only containers whose name contains this text.",
    )
    limit: int = Field(50, ge=1, le=500, description="Most containers to return.")


class ContainerRef(BaseModel):
    container: str = Field(
        description="Container name or id. A short id prefix is enough.",
    )


class ContainerName(BaseModel):
    """Arguments supplied by docker://containers/{name} templates."""

    name: str


class StopRequest(ContainerRef):
    seconds: int = Field(10, ge=0, le=600, description="How long to wait before killing it.")


class RemoveRequest(ContainerRef):
    force: bool = Field(False, description="Remove it even while it runs.")
    volumes: bool = Field(False, description="Remove its anonymous volumes too.")


class LogRequest(ContainerRef):
    lines: int = Field(100, ge=1, le=5000, description="How many last lines to read.")
    stream: Literal["both", "stdout", "stderr"] = Field("both", description="Which stream to read.")
    since: str | None = Field(
        None,
        description=(
            "Read only what was written after this time. Pass the `cursor` of "
            "an earlier read to see what is new since then."
        ),
    )
    contains: str | None = Field(
        None, description="Keep only lines holding this text. Case is ignored."
    )


class ExecRequest(ContainerRef):
    command: list[str] = Field(
        description='The command and its arguments, already split: ["ls", "-la", "/"].',
    )
    seconds: int = Field(
        30,
        ge=1,
        le=600,
        description="How long to let it run before giving up on it.",
    )
    workdir: str | None = Field(None, description="Directory to run it in.")
    user: str | None = Field(None, description='Who to run it as, such as "postgres".')


class ReadRequest(ContainerRef):
    path: str = Field(description="Absolute path of the file inside the container.")


class WriteRequest(ContainerRef):
    path: str = Field(description="Absolute path to write, inside the container.")
    content: str = Field(description="What the file is to hold. It replaces what is there.")
    mode: str = Field("644", description="Permission bits, as three or four octal digits.")


class FileContent(BaseModel):
    """File bytes read through the archive API, including from stopped containers."""

    container: str
    path: str
    size_bytes: int
    text: str
    truncated: bool = Field(False, description="True where the file was too large to return.")
    binary: bool = Field(False, description="True where this is not text. `text` is then empty.")


class Written(BaseModel):
    container: str
    path: str
    size_bytes: int
    note: str | None = None


class CreateRequest(BaseModel):
    """Container creation options; only image is required."""

    image: str = Field(description='Image to run, such as "redis:7-alpine".')
    name: str | None = Field(None, description="What to call it. Docker invents one otherwise.")
    command: list[str] | None = Field(
        None,
        description="What to run instead of the image's own command, already split.",
    )
    environment: dict[str, str] = Field(
        default_factory=dict, description="Variables to set inside it."
    )
    ports: list[str] = Field(
        default_factory=list,
        description='Ports to publish, as "host:container" or "host:container/udp".',
    )
    volumes: list[str] = Field(
        default_factory=list,
        description='Paths to mount, as "/on/host:/in/container" or with ":ro" appended.',
    )
    network: str | None = Field(None, description="Network to join. Otherwise the default one.")
    restart: Literal["no", "on-failure", "always", "unless-stopped"] = Field(
        "no", description="Whether the daemon starts it again after it stops."
    )
    workdir: str | None = Field(None, description="Directory its command starts in.")
    user: str | None = Field(None, description="Who its command runs as.")
    pull: Literal["missing", "always", "never"] = Field(
        "missing", description="When to download the image first."
    )
    start: bool = Field(True, description="Start it once created.")


class WaitRequest(ContainerRef):
    state: Literal["running", "exited", "healthy"] = Field(
        description="What to wait for it to become."
    )
    seconds: int = Field(30, ge=1, le=600, description="How long to wait before giving up.")


class ImageFilter(BaseModel):
    reference: str | None = Field(
        None, description='Keep only images matching this, such as "postgres*".'
    )
    unused: bool = Field(
        False, description="Keep only images no container uses. What is safe to remove."
    )


class ImageRef(BaseModel):
    image: str = Field(description='Image name, tag or id, such as "redis:7-alpine".')
    force: bool = Field(
        False, description="Remove it although a stopped container still refers to it."
    )


class PullRequest(BaseModel):
    image: str = Field(description='Image to pull, such as "redis:7-alpine".')


class PruneRequest(BaseModel):
    what: Literal["containers", "images", "volumes", "networks"] = Field(
        description="Which kind of unused thing to remove."
    )
    dangling_only: bool = Field(
        True,
        description=(
            "For images: remove only untagged ones. False removes every image "
            "no container uses, which is far more."
        ),
    )


class NameFilter(BaseModel):
    name: str | None = Field(None, description="Keep only those whose name contains this text.")


class EventFilter(BaseModel):
    seconds: int = Field(
        300, ge=1, le=86_400, description="How far back to look, in seconds from now."
    )
    container: str | None = Field(None, description="Keep only events about this container.")
    limit: int = Field(100, ge=1, le=1000, description="Most events to return, newest last.")


class Container(BaseModel):
    """One container, as much as is worth knowing without asking again."""

    id: str = Field(description="Short id, twelve characters.")
    name: str
    image: str
    state: str = Field(description="running, exited, paused, created, restarting or dead.")
    status: str = Field(description="Human wording, such as 'Up 3 hours'.")
    ports: list[str] = Field(default_factory=list, description="Published ports.")


class Containers(BaseModel):
    containers: list[Container]
    shown: int = Field(description="How many are listed here.")
    total: int = Field(description="How many matched, before the limit.")


class ContainerDetail(BaseModel):
    """Container inspect details; unlike list data, inspect has no human-readable status field."""

    id: str = Field(description="Short id, twelve characters.")
    name: str
    image: str
    state: str = Field(description="running, exited, paused, created, restarting or dead.")
    ports: list[str] = Field(default_factory=list, description="Published ports.")
    created: str = Field(description="When it was created, ISO-8601.")
    started: str | None = Field(None, description="When it last started, ISO-8601.")
    finished: str | None = Field(None, description="When it last stopped, ISO-8601.")
    command: str = Field(description="What it runs, quoted as a shell would.")
    restart_count: int
    restart_policy: str = Field("no", description="What the daemon does after it stops.")
    health: str | None = Field(None, description="healthy, unhealthy, starting, or absent.")
    exit_code: int | None = Field(None, description="Set once it has stopped.")
    error: str | None = None
    compose: str | None = Field(
        None,
        description='Its place in a Compose project, as "project/service", where it has one.',
    )
    networks: list[str] = Field(default_factory=list)
    mounts: list[str] = Field(default_factory=list, description="Source:destination pairs.")
    environment: list[str] = Field(
        default_factory=list, description="Names only. Values are not reported."
    )


class Image(BaseModel):
    id: str
    tags: list[str]
    size_mb: float
    created: str = Field(description="When it was built, ISO-8601.")
    used_by: list[str] = Field(
        default_factory=list,
        description="Containers holding it, running or not. Empty means safe to remove.",
    )


class Images(BaseModel):
    images: list[Image]
    total: int
    reclaimable_mb: float = Field(0.0, description="Size of the listed images no container uses.")


class Network(BaseModel):
    id: str
    name: str
    driver: str
    scope: str
    subnets: list[str] = Field(default_factory=list)
    containers: list[str] = Field(default_factory=list, description="What is attached.")


class Networks(BaseModel):
    networks: list[Network]
    total: int


class Volume(BaseModel):
    name: str
    driver: str
    mountpoint: str
    created: str | None = None
    used_by: list[str] = Field(
        default_factory=list, description="Containers mounting it. Empty means safe to remove."
    )


class Volumes(BaseModel):
    volumes: list[Volume]
    total: int


class LogLine(BaseModel):
    """Internal timestamped stream line used to merge stdout and stderr."""

    at: str | None = None
    stream: Stream
    text: str

    def rendered(self, *, tagged: bool) -> str:
        """Render a log line as text."""
        stamp = f"{self.at} " if self.at else ""
        mark = f"{'err' if self.stream == 'stderr' else 'out'} " if tagged else ""
        return f"{stamp}{mark}{self.text}"


class Logs(BaseModel):
    container: str
    lines: list[str] = Field(
        description=(
            'One line each, written as "<time> <text>". Where both streams '
            'were read, the stream stands between them: "<time> err <text>".'
        )
    )
    shown: int = Field(description="How many lines are here.")
    truncated: bool = Field(description="True where older lines were left out.")
    cursor: str | None = Field(
        None,
        description="Time of the last line. Pass it as `since` to read only what follows.",
    )


class Executed(BaseModel):
    """Command output and exit status, with stdout and stderr kept separate."""

    container: str
    command: str
    exit_code: int = Field(description="Zero is success. -1 means it never ran.")
    stdout: str
    stderr: str
    truncated: bool = Field(False, description="True where the output was too long to return.")
    timed_out: bool = Field(
        False,
        description="True where it outlasted `seconds`. It may still be running inside.",
    )


class Done(BaseModel):
    """Observed state after an action, including exit code and notes on unexpected outcomes."""

    container: str
    action: str
    state: str = Field(description="The state it is in now.")
    exit_code: int | None = Field(None, description="Set where it is stopped.")
    health: str | None = Field(None, description="healthy, unhealthy, starting, or absent.")
    note: str | None = Field(None, description="What went differently, where anything did.")


class Created(BaseModel):
    id: str = Field(description="Short id, twelve characters.")
    name: str
    image: str
    state: str
    ports: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list, description="What the daemon objected to.")
    note: str | None = None


class Stats(BaseModel):
    """Current resource use, with rates calculated from two counter samples."""

    container: str
    cpu_percent: float = Field(description="Of one core. 200 means two cores are busy.")
    memory_mb: float
    memory_limit_mb: float
    memory_percent: float
    net_rx_mb: float = Field(description="Received since it started.")
    net_tx_mb: float = Field(description="Sent since it started.")
    block_read_mb: float
    block_write_mb: float
    pids: int = Field(description="Processes running inside it.")


class Event(BaseModel):
    at: str = Field(description="When it happened, ISO-8601.")
    kind: str = Field(description="container, image, network, volume or daemon.")
    action: str = Field(description="start, die, destroy, pull, and so on.")
    subject: str = Field(description="Name of what it happened to.")
    exit_code: int | None = Field(None, description="Set on a container that died.")


class Events(BaseModel):
    events: list[Event]
    shown: int
    total: int = Field(description="How many matched, before the limit.")


class Pruned(BaseModel):
    what: str
    removed: list[str] = Field(default_factory=list)
    reclaimed_mb: float


class Info(BaseModel):
    """The daemon itself."""

    endpoint: str = Field(description="Where this server found it.")
    version: str
    api_version: str
    os: str
    architecture: str
    containers_running: int
    containers_total: int
    images: int


def short(value: str) -> str:
    """Twelve characters of an id, the length Docker's own output uses."""
    return value[:12]


def port_list(raw: Any) -> list[str]:
    """Published ports, as one readable string each."""
    published: list[str] = []
    for port in raw or []:
        if not isinstance(port, dict):
            continue
        public = port.get("PublicPort")
        private = port.get("PrivatePort")
        kind = port.get("Type", "tcp")
        if public:
            published.append(f"{port.get('IP', '0.0.0.0')}:{public}->{private}/{kind}")
        elif private:
            published.append(f"{private}/{kind}")
    return published
