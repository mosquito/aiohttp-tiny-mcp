"""Resource templates with handler arguments supplied by URI variables."""

from __future__ import annotations

from .container import container
from .logs import logs

__all__ = [
    "container",
    "logs",
]
