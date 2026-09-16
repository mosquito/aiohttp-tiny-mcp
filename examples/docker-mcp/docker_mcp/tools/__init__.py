"""Docker MCP tool handlers."""

from __future__ import annotations

from .container import container
from .containers import containers
from .create import create
from .events import events
from .exec import exec
from .images import images
from .info import info
from .logs import logs
from .networks import networks
from .prune import prune
from .pull import pull
from .read import read
from .remove import remove
from .remove_image import remove_image
from .restart import restart
from .results import outcome, settled
from .start import start
from .stats import stats
from .stop import stop
from .volumes import volumes
from .wait import wait
from .write import write

__all__ = [
    "container",
    "containers",
    "create",
    "events",
    "exec",
    "images",
    "info",
    "logs",
    "networks",
    "outcome",
    "prune",
    "pull",
    "read",
    "remove",
    "remove_image",
    "restart",
    "settled",
    "start",
    "stats",
    "stop",
    "volumes",
    "wait",
    "write",
]
