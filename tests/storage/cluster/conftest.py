from __future__ import annotations

import secrets
import socket
import sqlite3
import subprocess
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

HERE = Path(__file__).parent
TINY_NODE = HERE / "cluster_tiny_node.py"
SDK_NODE = HERE / "cluster_sdk_node.py"

START_SECONDS = 30.0


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
        cwd=str(HERE.parents[2]),
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
def cluster(tmp_path_factory) -> Iterator[Cluster]:
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
