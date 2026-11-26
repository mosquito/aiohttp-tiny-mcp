"""Docker MCP tool handlers."""

from __future__ import annotations

from .container import container
from .containers import containers
from .create import create
from .events import events
from .exec import exec, exec_unprivileged
from .images import images
from .info import info
from .logs import logs
from .networks import networks
from .prune import prune
from .pull import pull
from .read import read, read_unprivileged
from .remove import remove
from .remove_image import remove_image
from .restart import restart, restart_unprivileged
from .results import outcome, settled
from .start import start, start_unprivileged
from .stats import stats
from .stop import stop
from .volumes import volumes
from .wait import wait
from .write import write, write_unprivileged

__all__ = [
    "container",
    "containers",
    "create",
    "events",
    "exec",
    "exec_unprivileged",
    "images",
    "info",
    "logs",
    "networks",
    "outcome",
    "prune",
    "pull",
    "read",
    "read_unprivileged",
    "remove",
    "remove_image",
    "restart",
    "restart_unprivileged",
    "settled",
    "start",
    "start_unprivileged",
    "stats",
    "stop",
    "volumes",
    "wait",
    "write",
    "write_unprivileged",
]
