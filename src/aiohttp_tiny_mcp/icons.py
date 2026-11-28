"""Material Symbols presets stored as embedded light/dark icon pairs."""

from __future__ import annotations

from collections.abc import Sequence
from enum import Enum
from pathlib import Path

from .protocol.models import Icon

ICON_FILES = Path(__file__).with_name("data") / "icons"


class Icons(Enum):
    """Ready-to-use Material Symbols Rounded Fill icon pairs."""

    MAGNIFYING_GLASS = Icon.from_files(
        ICON_FILES / "magnifying-glass.png",
        ICON_FILES / "magnifying-glass-dark.png",
        sizes=["128x128"],
    )
    PLUS = Icon.from_files(ICON_FILES / "plus.png", ICON_FILES / "plus-dark.png", sizes=["128x128"])
    PEN = Icon.from_files(ICON_FILES / "pen.png", ICON_FILES / "pen-dark.png", sizes=["128x128"])
    TRASH_CAN = Icon.from_files(
        ICON_FILES / "trash-can.png", ICON_FILES / "trash-can-dark.png", sizes=["128x128"]
    )
    FLOPPY_DISK = Icon.from_files(
        ICON_FILES / "floppy-disk.png", ICON_FILES / "floppy-disk-dark.png", sizes=["128x128"]
    )
    FILE = Icon.from_files(ICON_FILES / "file.png", ICON_FILES / "file-dark.png", sizes=["128x128"])
    FOLDER = Icon.from_files(
        ICON_FILES / "folder.png", ICON_FILES / "folder-dark.png", sizes=["128x128"]
    )
    DATABASE = Icon.from_files(
        ICON_FILES / "database.png", ICON_FILES / "database-dark.png", sizes=["128x128"]
    )
    GEAR = Icon.from_files(ICON_FILES / "gear.png", ICON_FILES / "gear-dark.png", sizes=["128x128"])
    USER = Icon.from_files(ICON_FILES / "user.png", ICON_FILES / "user-dark.png", sizes=["128x128"])
    USERS = Icon.from_files(
        ICON_FILES / "users.png", ICON_FILES / "users-dark.png", sizes=["128x128"]
    )
    LOCK = Icon.from_files(ICON_FILES / "lock.png", ICON_FILES / "lock-dark.png", sizes=["128x128"])
    KEY = Icon.from_files(ICON_FILES / "key.png", ICON_FILES / "key-dark.png", sizes=["128x128"])
    LINK = Icon.from_files(ICON_FILES / "link.png", ICON_FILES / "link-dark.png", sizes=["128x128"])
    UPLOAD = Icon.from_files(
        ICON_FILES / "upload.png", ICON_FILES / "upload-dark.png", sizes=["128x128"]
    )
    DOWNLOAD = Icon.from_files(
        ICON_FILES / "download.png", ICON_FILES / "download-dark.png", sizes=["128x128"]
    )
    ARROWS_ROTATE = Icon.from_files(
        ICON_FILES / "arrows-rotate.png", ICON_FILES / "arrows-rotate-dark.png", sizes=["128x128"]
    )
    PLAY = Icon.from_files(ICON_FILES / "play.png", ICON_FILES / "play-dark.png", sizes=["128x128"])
    STOP = Icon.from_files(ICON_FILES / "stop.png", ICON_FILES / "stop-dark.png", sizes=["128x128"])
    CIRCLE_INFO = Icon.from_files(
        ICON_FILES / "circle-info.png", ICON_FILES / "circle-info-dark.png", sizes=["128x128"]
    )
    TRIANGLE_EXCLAMATION = Icon.from_files(
        ICON_FILES / "triangle-exclamation.png",
        ICON_FILES / "triangle-exclamation-dark.png",
        sizes=["128x128"],
    )
    CHECK = Icon.from_files(
        ICON_FILES / "check.png", ICON_FILES / "check-dark.png", sizes=["128x128"]
    )
    XMARK = Icon.from_files(
        ICON_FILES / "xmark.png", ICON_FILES / "xmark-dark.png", sizes=["128x128"]
    )
    CODE = Icon.from_files(ICON_FILES / "code.png", ICON_FILES / "code-dark.png", sizes=["128x128"])
    BOX = Icon.from_files(
        ICON_FILES / "box.png",
        ICON_FILES / "box-dark.png",
        sizes=["128x128"],
    )
    LAYER_GROUP = Icon.from_files(
        ICON_FILES / "layer-group.png",
        ICON_FILES / "layer-group-dark.png",
        sizes=["128x128"],
    )
    NETWORK_WIRED = Icon.from_files(
        ICON_FILES / "network-wired.png",
        ICON_FILES / "network-wired-dark.png",
        sizes=["128x128"],
    )
    HARD_DRIVE = Icon.from_files(
        ICON_FILES / "hard-drive.png",
        ICON_FILES / "hard-drive-dark.png",
        sizes=["128x128"],
    )
    TERMINAL = Icon.from_files(
        ICON_FILES / "terminal.png",
        ICON_FILES / "terminal-dark.png",
        sizes=["128x128"],
    )
    FILE_LINES = Icon.from_files(
        ICON_FILES / "file-lines.png",
        ICON_FILES / "file-lines-dark.png",
        sizes=["128x128"],
    )
    CHART_LINE = Icon.from_files(
        ICON_FILES / "chart-line.png",
        ICON_FILES / "chart-line-dark.png",
        sizes=["128x128"],
    )
    HOURGLASS_HALF = Icon.from_files(
        ICON_FILES / "hourglass-half.png",
        ICON_FILES / "hourglass-half-dark.png",
        sizes=["128x128"],
    )
    BROOM = Icon.from_files(
        ICON_FILES / "broom.png",
        ICON_FILES / "broom-dark.png",
        sizes=["128x128"],
    )
    COMMENTS = Icon.from_files(
        ICON_FILES / "comments.png",
        ICON_FILES / "comments-dark.png",
        sizes=["128x128"],
    )
    HASHTAG = Icon.from_files(
        ICON_FILES / "hashtag.png",
        ICON_FILES / "hashtag-dark.png",
        sizes=["128x128"],
    )
    PAPER_PLANE = Icon.from_files(
        ICON_FILES / "paper-plane.png",
        ICON_FILES / "paper-plane-dark.png",
        sizes=["128x128"],
    )
    INBOX = Icon.from_files(
        ICON_FILES / "inbox.png",
        ICON_FILES / "inbox-dark.png",
        sizes=["128x128"],
    )
    CLOCK_ROTATE_LEFT = Icon.from_files(
        ICON_FILES / "clock-rotate-left.png",
        ICON_FILES / "clock-rotate-left-dark.png",
        sizes=["128x128"],
    )
    BELL = Icon.from_files(
        ICON_FILES / "bell.png",
        ICON_FILES / "bell-dark.png",
        sizes=["128x128"],
    )
    BELL_SLASH = Icon.from_files(
        ICON_FILES / "bell-slash.png",
        ICON_FILES / "bell-slash-dark.png",
        sizes=["128x128"],
    )
    REPLY = Icon.from_files(
        ICON_FILES / "reply.png",
        ICON_FILES / "reply-dark.png",
        sizes=["128x128"],
    )


def expand_icons(given: Sequence[Icon | Icons] | None) -> tuple[Icon, ...] | None:
    """Resolve presets at registration. Custom icons keep their original metadata."""
    if given is None:
        return None
    return tuple(
        icon
        for item in given
        for icon in (
            tuple(preset.model_copy(deep=True) for preset in item.value)
            if isinstance(item, Icons)
            else (item,)
        )
    )
