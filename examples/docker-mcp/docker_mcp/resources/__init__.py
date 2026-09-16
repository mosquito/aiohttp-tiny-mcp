"""Fixed and templated Docker resources registered by server.py."""

from __future__ import annotations

from . import templates
from .containers import containers
from .images import images
from .info import info

__all__ = [
    "containers",
    "images",
    "info",
    "templates",
]
