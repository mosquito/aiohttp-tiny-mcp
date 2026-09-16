"""Translate Docker failures into actionable tool errors."""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from aiodocker.exceptions import DockerError

STOPPED = "is not running"


class Trouble(Exception):
    """Base error; subclasses name the failure in tool results."""


class NotFound(Trouble, LookupError):
    """No container, image, network or volume matches."""


class NotRunning(Trouble):
    """The container exists but is stopped, and the action needs it running."""


class Conflict(Trouble):
    """The daemon refused: a name is taken, or something is still in use."""


class Unreachable(Trouble):
    """The daemon did not answer. Nothing was done."""


class Timeout(Trouble):
    """The work outlasted the time it was given. It may still be running."""


class Refused(Trouble):
    """A person was asked and said no, or was not asked at all."""


LONG_ID = re.compile(r"\b[0-9a-f]{32,64}\b")


def said(error: DockerError) -> str:
    """The daemon's own sentence, with its full ids cut to the twelve
    characters its own output uses."""
    text = str(error.message or "").strip().rstrip(".")
    return LONG_ID.sub(lambda found: found.group(0)[:12], text)


@asynccontextmanager
async def clear(doing: str, *, subject: str = "") -> AsyncIterator[None]:
    """Translate Docker errors. doing is a present-participle action; subject identifies its
    target.
    """
    about = f" {subject}" if subject else ""
    try:
        yield
    except DockerError as error:
        if error.status == 404:
            said_plainly = f"{subject} was not found" if subject else f"not found: {said(error)}"
            raise NotFound(said_plainly) from error
        if error.status == 409 and STOPPED in said(error):
            raise NotRunning(
                f"{subject or 'the container'} is stopped. Start it first, then try {doing} again."
            ) from error
        if error.status == 409:
            raise Conflict(f"the daemon refused{about}: {said(error)}") from error
        if error.status in (502, 503, 504):
            raise Unreachable(f"the daemon is not answering while {doing}") from error
        raise Trouble(f"{doing}{about} failed: {said(error)}") from error
    except OSError as error:
        raise Unreachable(
            f"cannot reach the Docker daemon while {doing}: {error}. "
            "Check that it runs, and that this server may read its socket."
        ) from error


__all__ = [
    "Conflict",
    "NotFound",
    "NotRunning",
    "Refused",
    "Timeout",
    "Trouble",
    "Unreachable",
    "clear",
]
