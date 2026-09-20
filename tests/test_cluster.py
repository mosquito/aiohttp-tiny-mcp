"""Two stacks, four server processes, one shared store.

Two aiohttp-tiny-mcp nodes and two official-SDK nodes run as separate
processes over one SQLite file. The client then starts work on one node of a
pair and finishes it on the other, which is what a load balancer does to a
deployment that keeps no affinity.

The tests record what each stack shares and what it does not:

- Round-trip state crosses nodes on both stacks, by different means. This
  package stores the state and hands out an id; the SDK seals the state into a
  token the client carries, so its nodes must share a key.
- Subscription events cross nodes on both stacks, through a pluggable fan-out.
- A legacy session crosses nodes on this package only. The SDK holds its
  stateful transports in a per-process dictionary, so the second node answers
  "Session not found" even when both nodes share an event store.

The shared backends live in tests/cluster_storage.py; the node programs in
tests/cluster_tiny_node.py and tests/cluster_sdk_node.py.
"""

from __future__ import annotations

import asyncio
import secrets
import socket
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

from aiohttp_tiny_mcp import Client, ClientError, elicit_accept
from aiohttp_tiny_mcp.models import CallToolResult
from aiohttp_tiny_mcp.protocol.selection import AdapterSet

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(60)]

MODERN = AdapterSet.default().by_version["2026-07-28"]
LEGACY = AdapterSet.default().by_version["2025-11-25"]

HERE = Path(__file__).parent
TINY_NODE = HERE / "cluster_tiny_node.py"
SDK_NODE = HERE / "cluster_sdk_node.py"

CONFIG_URI = "config://app"

START_SECONDS = 30.0

ARRIVE_SECONDS = 15.0


@dataclass
class Cluster:
    """The running nodes and the file they share."""

    nodes: dict[str, Node]
    db: Path

    def __getitem__(self, name: str) -> Node:
        return self.nodes[name]

    def rows(self, prefix: str) -> int:
        """Count stored events whose topic starts with `prefix`."""
        with sqlite3.connect(self.db) as db:
            return db.execute(
                "SELECT COUNT(*) FROM mcp_events WHERE topic LIKE ?", (f"{prefix}%",)
            ).fetchone()[0]


@dataclass
class Node:
    """One server process."""

    name: str
    url: str
    process: subprocess.Popen
    log: Path

    def report(self) -> str:
        return f"{self.name} log:\n{self.log.read_text()}"


def free_port() -> int:
    with socket.socket() as held:
        held.bind(("127.0.0.1", 0))
        return held.getsockname()[1]


def accepting(port: int) -> bool:
    with socket.socket() as probe:
        probe.settimeout(0.2)
        return probe.connect_ex(("127.0.0.1", port)) == 0


def start(name: str, script: Path, db: Path, root: Path, extra: list[str]) -> Node:
    port = free_port()
    log = root / f"{name}.log"
    handle = log.open("w")
    process = subprocess.Popen(
        [sys.executable, str(script), "--db", str(db), "--port", str(port), *extra],
        stdout=handle,
        stderr=subprocess.STDOUT,
        cwd=str(HERE.parent),
    )
    return Node(name=name, url=f"http://127.0.0.1:{port}/mcp", process=process, log=log)


def await_ready(node: Node) -> None:
    port = int(node.url.rsplit(":", 1)[1].split("/")[0])
    deadline = time.monotonic() + START_SECONDS
    while time.monotonic() < deadline:
        if node.process.poll() is not None:
            raise RuntimeError(f"{node.name} exited early.\n{node.report()}")
        if accepting(port):
            return
        time.sleep(0.1)
    raise RuntimeError(f"{node.name} never accepted connections.\n{node.report()}")


@pytest.fixture(scope="module")
def cluster(tmp_path_factory) -> Cluster:
    """Four nodes that share a store, plus one SDK node that shares nothing."""
    root = tmp_path_factory.mktemp("cluster")
    db = root / "state.sqlite"
    key = secrets.token_hex(32)
    plan = [
        ("tiny_a", TINY_NODE, []),
        ("tiny_b", TINY_NODE, []),
        ("sdk_a", SDK_NODE, ["--key", key]),
        ("sdk_b", SDK_NODE, ["--key", key]),
        ("sdk_alone", SDK_NODE, ["--ephemeral-state"]),
    ]
    nodes = {name: start(name, script, db, root, extra) for name, script, extra in plan}
    try:
        for node in nodes.values():
            await_ready(node)
        yield Cluster(nodes=nodes, db=db)
    finally:
        for node in nodes.values():
            node.process.terminate()
        for node in nodes.values():
            try:
                node.process.wait(timeout=10)
            except subprocess.TimeoutExpired:  # pragma: no cover - a wedged node
                node.process.kill()


async def ask(url: str) -> tuple[str, str]:
    """Call `confirm` until it asks, and return the question name with the state."""
    async with Client(url, MODERN) as client:
        asked = await client.call_tool("confirm", {"service": "web"})
    assert isinstance(asked, dict), asked
    requests, state = MODERN.client_input_requests(asked)
    return next(iter(requests)), state


async def answer(url: str, name: str, state: str) -> CallToolResult:
    """Finish the round trip that `ask` started, possibly on another node."""
    async with Client(url, MODERN) as client:
        done = await client.call_tool(
            "confirm",
            {"service": "web"},
            input_responses={name: elicit_accept({"ok": True})},
            request_state=state,
        )
    assert isinstance(done, CallToolResult), done
    return done


def said(result: CallToolResult) -> str:
    return "".join(getattr(part, "text", "") for part in result.content)


async def resumes(first: str, second: str) -> str:
    name, state = await ask(first)
    return said(await answer(second, name, state))


async def watch(listener: Client, actor: Client) -> dict:
    """Subscribe on one node, act on the other, and return the event that arrives."""
    arrived: asyncio.Queue[dict] = asyncio.Queue()

    async def relay() -> None:
        async for frame in listener.listen(resources=[CONFIG_URI]):
            await arrived.put(frame)

    task = asyncio.ensure_future(relay())
    try:
        deadline = time.monotonic() + ARRIVE_SECONDS
        while not listener.accepted and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        assert listener.accepted.get("resourceSubscriptions") == [CONFIG_URI]
        await actor.call_tool("touch", {})
        return await asyncio.wait_for(arrived.get(), ARRIVE_SECONDS)
    finally:
        task.cancel()


async def pid(url: str) -> int:
    async with Client(url, MODERN) as client:
        return int(said(await client.call_tool("whoami", {})))


@pytest.mark.parametrize("pair", [("tiny_a", "tiny_b"), ("sdk_a", "sdk_b")])
async def test_each_pair_answers_from_two_processes(cluster, pair):
    """Prove the premise of every test below: the nodes are separate processes."""
    answered = {await pid(cluster[name].url) for name in pair}
    assert len(answered) == 2
    assert answered == {cluster[name].process.pid for name in pair}


async def test_one_client_uses_either_node_of_this_package(cluster):
    """A legacy session opened on one node is honored by the other."""
    async with Client(cluster["tiny_a"].url, LEGACY) as opened:
        await opened.initialize()
        held = opened.session_id
    assert held is not None
    async with Client(cluster["tiny_b"].url, LEGACY) as elsewhere:
        elsewhere.session_id = held
        done = await elsewhere.call_tool("add", {"a": 2, "b": 3})
    assert said(done) == "5"


async def test_an_sdk_legacy_session_stays_on_the_node_that_made_it(cluster):
    """A shared event store does not make a stateful SDK session portable.

    The SDK keeps its stateful transports in a per-process dictionary, so the
    second node answers 404 as if the session never existed. This is the behavior
    to design around, not a defect: the SDK documents affinity for this mode.
    The client recovers by opening a new session on the second node, so the
    call succeeds, but on a session id the first node never issued.
    """
    async with Client(cluster["sdk_a"].url, LEGACY) as opened:
        await opened.initialize()
        held = opened.session_id
        assert said(await opened.call_tool("add", {"a": 2, "b": 3})) == "5"
    assert held is not None
    assert cluster.rows("sdk-stream/") > 0
    async with Client(cluster["sdk_b"].url, LEGACY) as elsewhere:
        elsewhere.session_id = held
        assert said(await elsewhere.call_tool("add", {"a": 2, "b": 3})) == "5"
        assert elsewhere.session_id != held


async def test_this_package_finishes_a_round_trip_on_another_node(cluster):
    """The state is an id into the shared store, so any node can spend it."""
    assert await resumes(cluster["tiny_a"].url, cluster["tiny_b"].url) == "deployed web"


async def test_the_sdk_finishes_a_round_trip_on_another_node(cluster):
    """The state is a sealed token, so any node holding the key can read it."""
    assert await resumes(cluster["sdk_a"].url, cluster["sdk_b"].url) == "deployed web"


async def test_the_sdk_refuses_state_from_a_node_it_shares_no_key_with(cluster):
    """Sharing the store is not enough for the SDK; the key is what carries state."""
    name, state = await ask(cluster["sdk_a"].url)
    async with Client(cluster["sdk_alone"].url, MODERN) as stranger:
        with pytest.raises(ClientError, match="requestState"):
            await stranger.call_tool(
                "confirm",
                {"service": "web"},
                input_responses={name: elicit_accept({"ok": True})},
                request_state=state,
            )


async def test_this_package_delivers_a_subscription_from_another_node(cluster):
    async with (
        Client(cluster["tiny_a"].url, MODERN) as listener,
        Client(cluster["tiny_b"].url, MODERN) as actor,
    ):
        frame = await watch(listener, actor)
    assert frame["method"] == "notifications/resources/updated"
    assert frame["params"]["uri"] == CONFIG_URI


async def test_the_sdk_delivers_a_subscription_from_another_node(cluster):
    """A `SubscriptionBus` over the shared store fans events across processes."""
    async with (
        Client(cluster["sdk_a"].url, MODERN) as listener,
        Client(cluster["sdk_b"].url, MODERN) as actor,
    ):
        frame = await watch(listener, actor)
    assert frame["method"] == "notifications/resources/updated"
    assert frame["params"]["uri"] == CONFIG_URI


async def test_both_stacks_resume_across_nodes_at_the_same_time(cluster):
    """One client, four nodes, two implementations, all in flight together."""
    rounds = [
        resumes(cluster["tiny_a"].url, cluster["tiny_b"].url),
        resumes(cluster["tiny_b"].url, cluster["tiny_a"].url),
        resumes(cluster["sdk_a"].url, cluster["sdk_b"].url),
        resumes(cluster["sdk_b"].url, cluster["sdk_a"].url),
    ]
    assert await asyncio.gather(*rounds) == ["deployed web"] * 4
