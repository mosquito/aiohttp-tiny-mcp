# docker-mcp

An MCP server for Docker, built on
[aiohttp-tiny-mcp](https://github.com/mosquito/aiohttp-tiny-mcp).

It is here as a worked example of a complete server: a real client library
underneath, a real store and event source, a command line, and tests that run
where Docker is not installed. Every protocol revision the package speaks is
served by the same handlers.

## Running it

```bash
uv run docker-mcp                       # http://127.0.0.1:8080/mcp
uv run docker-mcp --stdio               # for a locally-installed client
uv run docker-mcp --port 9000 --state /var/lib/docker-mcp/state.sqlite3
```

Every setting can also come from the environment or a config file. Three files
are read where they exist: `/etc/docker/mcp.ini`, then `~/.config/docker/mcp.ini`,
then `.docker-mcp.ini` in this directory, each overriding the one before.
`--config FILE` overrides all three. The order of precedence is defaults, then
those files, then `DOCKER_MCP_*`, then the command line:

```bash
DOCKER_MCP_PORT=9000 DOCKER_MCP_STATE=/tmp/state.sqlite3 uv run docker-mcp
```

`--help` lists everything, including which environment variable each setting
reads. `--generate-config` writes every setting, its help and its default to a
config file, so a deployment edits that file instead of a command line:

```bash
uv run docker-mcp --generate-config /etc/docker/mcp.ini
uv run docker-mcp --generate-config -        # to standard output
```

Top-level settings go under `[DEFAULT]`, and a group of settings has its own
section, such as `[jwt]`. A list is written as a Python literal, in the config
file and in the environment alike:

```ini
allowed_origin = ["https://docker.example.com"]
```

```bash
DOCKER_MCP_ALLOWED_ORIGIN='["https://docker.example.com"]' uv run docker-mcp
```

## Unprivileged mode

Enable runtime checks for host access with `--unprivileged`:

```bash
uv run docker-mcp --unprivileged
uv run docker-mcp --unprivileged --network sandbox
```

The `create` tool hides and rejects `network`. Its `volumes` argument accepts
only named volumes, such as `data:/data`, `data:/data:rw` or `data:/data:ro`.
Host paths, Docker socket mounts, extra mount flags and unknown creation
arguments are rejected. Missing named volumes are created with the `local`
driver. Existing volumes must use `local` without driver options, because
[volume options can mount host paths](https://docs.docker.com/engine/storage/volumes/).
Named volumes persist after their containers are removed.

For example, call `create` with:

```json
{"image": "redis:7-alpine", "name": "cache", "volumes": ["cache-data:/data"]}
```

Before `read`, `write`, `exec`, `start` or `restart`, the server inspects the
target container. It rejects host mounts, extra capabilities or devices,
shared host namespaces, custom runtimes and relaxed security settings.
New containers use `no-new-privileges` and the default bridge network unless
the operator selects another local bridge network or `none`.
Published ports remain available.

Equivalent settings are `DOCKER_MCP_UNPRIVILEGED=true` or `unprivileged=true`
in the config file. Python callers can use `build(registry, client, unprivileged=True)`.

This mode restricts Docker configuration exposed through MCP. It does not
filter container network traffic, provide per-user volume isolation or
protect against Docker/kernel vulnerabilities and concurrent changes by
another Docker administrator. Use a dedicated daemon for untrusted workloads.

## Fixed network

`--network NAME` works independently of `--unprivileged`:

```bash
uv run docker-mcp --network sandbox
```

The network must already exist. The server uses it for new containers,
hides and rejects `create.network`, omits the `networks` tool and removes
`networks` from `prune.what`. This option does not enable unprivileged mode.

Equivalent settings are `DOCKER_MCP_NETWORK=sandbox` or `network=sandbox`
in the config file. Python callers can use `build(registry, client, network="sandbox")`.
Add `--unprivileged` separately when both restrictions are needed.

## Authentication

`--jwt-psk` verifies an HS256 JWT on every HTTP request. The audience is this
endpoint's public URL: a token must name it, and the endpoint publishes it as
its protected resource.

```bash
uv run docker-mcp --jwt-psk "$KEY" \
    --jwt-audience https://docker.example.com/mcp \
    --jwt-issuer https://docker.example.com
```

The key needs at least 32 bytes. It is a secret argument: its `repr` is
`******`, so a log of the settings does not hold it. The config file or
`DOCKER_MCP_JWT_PSK` keeps it out of the command line, which another user on
the machine can read. `--jwt-issuer` is optional; an empty issuer accepts a
token from any issuer.

These three settings are one group, and the config file holds them in one
section:

```ini
[jwt]
psk = a-key-of-at-least-32-random-bytes
audience = https://docker.example.com/mcp
issuer = https://docker.example.com
```

Mint a token with the same key, and send it as `Authorization: Bearer <token>`:

<!-- name: async test_readme_jwt -->
```python
import time

import jwt

from docker_mcp.__main__ import Parser, authentication

key = "replace-this-example-key-with-32-random-bytes"
parser = Parser()
parser.parse_args(
    [
        "--jwt-psk",
        key,
        "--jwt-audience",
        "https://docker.example.com/mcp",
        "--jwt-issuer",
        "https://docker.example.com",
    ]
)
auth = authentication(parser)

token = jwt.encode(
    {
        "sub": "laptop",
        "iss": "https://docker.example.com",
        "aud": "https://docker.example.com/mcp",
        "exp": time.time() + 86400,
    },
    key,
    algorithm="HS256",
)
assert (await auth.principal(f"Bearer {token}")).subject == "laptop"
```

`--jwt-query-token` also accepts the token as the `token` query argument, for
a client that sends no header:

```
https://docker.example.com/mcp?token=<token>
```

The header wins where both are present. The URL is then a credential: it
reaches browser history, the client's saved settings and every log that keeps
the query, so the option is off by default. With it on, the server installs
the library's access-log filter, which writes `token=[REDACTED]`.

A token expires, and the server rejects one without `exp`. Revocation is
collective: replace the key and restart the server. This is a credential on
the wire, so terminate TLS in front of the server. The policy protects the MCP
route; the console page itself stays public and asks for the token in its
dialog. `--stdio` ignores the key, because one process serves one caller.

Other deployments need other identity: `Registry(auth=...)` also takes Basic
authentication, an OAuth protected resource, or a policy of your own. See
[authentication](https://mosquito.github.io/aiohttp-tiny-mcp/guide/auth.html).

## What it offers

Enough on its own. An agent given this server and nothing else can find a
container, read its files and its log, run a command in it, change it, and
start a new one.

| Tool | Does |
| --- | --- |
| `containers` | List containers, filtered by name and by whether they run |
| `container` | One container in full: health, exit code, restarts, mounts, Compose service |
| `logs` | What it wrote, dated, with a cursor for reading only what is new |
| `read` | One file out of it -- running or not, and with no shell in the image |
| `images` | Images with sizes, and what still holds each one |
| `networks`, `volumes` | What is attached, and what mounts what |
| `events` | What the daemon did lately: started, died, removed |
| `stats` | Processor, memory, network and disk, as rates |
| `info` | Which daemon this is, its version, and how much it holds |
| `create` | Make one and start it -- `docker run`, with the flags named |
| `start`, `stop`, `restart` | The obvious things, reporting the state that followed |
| `wait` | Hold until it is running, stopped, or healthy |
| `exec` | Run a command inside it -- **asks unless the command only reads** |
| `write` | Put a file into it, running or not |
| `remove`, `remove_image`, `prune` | Reclaim space -- **each asks first** |
| `pull` | Download an image, reporting progress |

Resources: `docker://info`, `docker://containers`,
`docker://containers/{name}`, `docker://containers/{name}/logs`,
`docker://images`.

Prompts: `diagnose` for a container that misbehaves, `reclaim` for finding
space. Container and image names are suggested while either is filled in.

## The parts worth copying

**Handlers are plain functions.** Nothing in `tools/` knows about a registry.
Each is a coroutine over a connected `aiodocker.Docker`, so a test calls it
directly:

<!-- name: async test_readme_direct -->
```python
from docker_mcp import tools
from docker_mcp.models import ContainerFilter


async def show(client):
    listed = await tools.containers(ContainerFilter(all=True), client)
    return [item.name for item in listed.containers]
```

**One file per tool, holding the whole tool.** `tools/stats.py` has the two
samples, the arithmetic between them, and the handler -- open it and there is
nothing else to find. Nothing was factored out for its own sake: a helper
moves to `daemon.py` only where a second tool calls it, which is true of six
functions in all.

```
daemon.py         what more than one tool asks the daemon
tools/            one file per tool, plus results.py
resources/        addressed rather than called
resources/templates/   the ones whose address a client fills in
```

What a tool may do is read from its signature, not from where its file sits. A
handler taking only `(args, client)` changes nothing and can ask nobody; one
taking `ex: Exchange` may put a question; one taking `policy: Policy` may
destroy something. That is one place to look, and it is in the file already.

There is no wrapper around the daemon. `aiodocker.Docker` is the connection,
the application opens one and hands it to the registry, and a handler takes it
as `client: Docker`. Several functions in `daemon.py` never see it at all -- a
container reads its own log -- which a wrapper hid.

`templates/` is apart because a fixed resource can be listed and subscribed
to, a template cannot be listed at all, and a client only discovers that
difference by failing.

`server.py` registers handlers and selects deployment restrictions.
`safety.py` checks Docker configuration at runtime in unprivileged mode:

<!-- name: async test_readme_inventory -->
```python
from aiodocker import Docker
from aiohttp_tiny_mcp import MemoryHub, MemorySessionStore, Registry

from docker_mcp.server import build

# `build` says what is offered and nothing else. Reading it is reading the
# whole surface -- the daemon is never asked anything until a tool is called.
client = Docker()
try:
    registry = build(
        Registry("docker", "0.1.0", hub=MemoryHub(), session_store=MemorySessionStore()),
        client,
    )
    assert {"containers", "logs", "read", "exec", "write", "create"} <= set(registry.tools)
finally:
    await client.close()
```

**Asking is rationed, and the deployment sets the rate.** A server that asks
before everything is a server whose questions nobody reads -- and there is
usually a host in front of this one asking its own permission for every call,
so a second question buys only the habit of clicking through both. Four things
cut it down, in order: a policy decides which actions are worth a question at
all, a command that only reads never asks, an answer that said "do not ask
again" is honoured, and what is left asks once.

```bash
uv run docker-mcp                      # asks only before what cannot be undone
uv run docker-mcp --confirm never      # asks nothing; the host in front already does
uv run docker-mcp --confirm changes    # asks before anything that changes anything
```

Under the default, `create` and `write` do not interrupt: creating is undone
by removing, and a file written into a container goes away with it. `remove`,
`remove_image`, `prune` and a command that is not merely reading do:

<!-- name: async test_readme_consent -->
```python
from docker_mcp.consent import Policy, only_reads

assert only_reads(["cat", "/etc/hosts"])
assert not only_reads(["sh", "-c", "rm -rf /"])
assert not only_reads(["find", "/app", "-delete"])

assert Policy().asks("remove") and not Policy().asks("write")
assert not Policy("never").asks("remove")
assert Policy("changes").asks("write")
```

What was allowed is kept in the session store the package already has, so a
second worker honours a permission the first one was given. `2026-07-28` keeps
no session of its own, so the answer is filed under the caller the revision
states on every request -- otherwise "do not ask again in this session" would
be forgotten before the next call, which is worse than never offering it.
`ex.ask` reaches a client through MRTR on `2026-07-28`, a pushed request on
`2025-11-25` and `2025-06-18`, and the tool call itself on `2025-03-26`. The
handler says one line and knows none of that.

**Failures are said in words, not status numbers.** `errors.py` translates
Docker failures. A command sent to a stopped container came
back as `DockerError: [409] container 3156cbe0b5f6... is not running`, which
names an id nobody typed and does not say what to do:

<!-- name: async test_readme_errors -->
```python
import pytest
from aiodocker.exceptions import DockerError

from docker_mcp.errors import NotRunning, clear


async def check():
    with pytest.raises(NotRunning, match="Start it first"):
        async with clear("running a command", subject="cache"):
            raise DockerError(409, "container 3156cbe0b5f6 is not running")
```

**A change reports the state that followed it.** Docker answers `start`
before the process is up, so a container that starts and dies at once would
be reported as started. Every changing tool reads the state a moment later,
and says what went differently:

<!-- name: async test_readme_outcome -->
```python
from docker_mcp import tools

done = tools.outcome("web", "start", {"Status": "exited", "ExitCode": 3}, "running")
assert done.exit_code == 3
assert "did not stay running" in done.note
```

**The application owns the connection.** `__main__.py` opens one
`aiodocker.Docker`, hands it over with `registry.provide_instance(client)`, and
closes it on the way out. No lazy opening, no second lifetime to keep in step
with the first:

```python
client = Docker(url=parser.docker_url) if parser.docker_url else Docker()
return build(registry, client, Policy(parser.confirm)), storage, client
```

**State is SQLite.** `aiohttp_tiny_mcp.storage.sqlite` holds the sessions and the
events in one file, which is what several workers on one machine share. This
example used to carry its own copy of that; the package ships it now, and
`aiohttp-tiny-mcp[sqlite]` is what brings it. Across machines the same two
contracts are served by `aiohttp_tiny_mcp.storage.redis` or `.postgres`.

**The daemon's own events become notifications.** A container that dies is
published to the hub, and whoever subscribed hears about it.

## Tests

```bash
uv run pytest
```

They need no Docker. `tests/fake_docker.py` is an aiohttp application
answering the endpoints these tools use, so the real `aiodocker`, the real
SQLite store, the real endpoint and a real MCP client are all exercised --
only the daemon is a stand-in.

Two layers, deliberately:

- `test_functions.py` calls the handlers directly. Fast, and it says what
  broke.
- `test_reading.py`, `test_changing.py` and `test_events.py` drive a real
  client over HTTP, once per protocol revision where the revision matters.
- `test_real_daemon.py` lives one container end to end against a real daemon:
  create, wait, exec, write, read, logs, stats, events, stop, remove. It is
  skipped where Docker is not reachable, and it is what caught `since` on the
  log endpoint taking a Unix timestamp while every line it returns is dated
  RFC 3339.

To run against a real daemon instead, point the tests at it:

```bash
DOCKER_MCP_DOCKER_URL=unix:///var/run/docker.sock uv run pytest -k real
```
