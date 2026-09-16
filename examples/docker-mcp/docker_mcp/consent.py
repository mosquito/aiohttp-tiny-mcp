"""Confirmation policy and remembered permissions.

Check the deployment policy, read-only command allowlist, and saved grants before asking. The
default confirms destructive actions; `never` disables confirmation and `changes` confirms all
changes. Grants use shared session storage.
"""

from __future__ import annotations

import hashlib
from typing import Any

from aiohttp_tiny_mcp import Exchange, elicit
from aiohttp_tiny_mcp.namespaces import scoped
from aiohttp_tiny_mcp.sessions import DATA_KEY, Session

CONSENT_KEY = "consent"

DESTROYS = frozenset({"exec", "prune", "remove", "remove_image"})

LEVELS = ("never", "destructive", "changes")

CALLER_PREFIX = "consent-"

CONFIRM: dict[str, Any] = {
    "type": "object",
    "properties": {"confirmed": {"type": "boolean", "description": "Say yes to go ahead."}},
    "required": ["confirmed"],
}

REMEMBER: dict[str, Any] = {
    "type": "string",
    "enum": ["once", "this", "any"],
    "enumNames": [
        "Ask me again next time",
        "Do not ask again for this one",
        "Do not ask again for anything like it",
    ],
    "default": "once",
    "description": "How long this answer holds.",
}


class Policy:
    """Deployment-wide confirmation policy injected into handlers."""

    def __init__(self, level: str = "destructive") -> None:
        if level not in LEVELS:
            raise ValueError(f"confirm must be one of {', '.join(LEVELS)}, not {level!r}")
        self.level = level

    def asks(self, action: str) -> bool:
        if self.level == "never":
            return False
        if self.level == "changes":
            return True
        return action in DESTROYS


def question(text: str, *, keepable: bool) -> Any:
    """Build a confirmation form, including remembrance only when it can be persisted."""
    schema = {**CONFIRM, "properties": dict(CONFIRM["properties"])}
    if keepable:
        schema["properties"]["remember"] = REMEMBER
    return elicit(text, schema)


READING = frozenset(
    {
        "arp",
        "basename",
        "cat",
        "cksum",
        "cut",
        "date",
        "df",
        "dirname",
        "dmesg",
        "du",
        "echo",
        "egrep",
        "env",
        "false",
        "fgrep",
        "file",
        "find",
        "free",
        "getent",
        "grep",
        "head",
        "hostname",
        "id",
        "ifconfig",
        "ip",
        "locale",
        "ls",
        "lscpu",
        "lsof",
        "md5sum",
        "mount",
        "nl",
        "nproc",
        "netstat",
        "printenv",
        "printf",
        "ps",
        "pwd",
        "readlink",
        "sha1sum",
        "sha256sum",
        "sort",
        "ss",
        "stat",
        "strings",
        "tail",
        "top",
        "true",
        "uname",
        "uniq",
        "uptime",
        "wc",
        "which",
        "whoami",
    }
)

WRITING_ARGUMENTS: dict[str, frozenset[str]] = {
    "find": frozenset(
        {
            "-delete",
            "-exec",
            "-execdir",
            "-fls",
            "-fprint",
            "-fprint0",
            "-fprintf",
            "-ok",
            "-okdir",
        }
    ),
}


def only_reads(command: list[str]) -> bool:
    """Check the command allowlist and reject arguments with side effects. Unlisted commands
    require confirmation.
    """
    if not command:
        return False
    program = command[0].rsplit("/", 1)[-1]
    if program not in READING:
        return False
    forbidden = WRITING_ARGUMENTS.get(program, frozenset())
    return not any(argument in forbidden for argument in command[1:])


def scope(action: str, target: str) -> str:
    """The permission one answer grants, as one string."""
    return f"{action}:{target}"


def caller(ex: Exchange) -> str | None:
    """Derive a stable permission handle from self-declared client identity on revisions without
    sessions.

    This is not authentication: endpoint callers can already invoke tools and answer their own
    confirmations. Grants expire with the session.
    """
    info = getattr(ex, "client_info", None)
    said = f"{getattr(info, 'name', '')}/{getattr(info, 'version', '')}".strip("/")
    if not said:
        return None
    return CALLER_PREFIX + hashlib.sha256(said.encode()).hexdigest()[:32]


async def remembering(ex: Exchange) -> Session | None:
    """Use the protocol session or a caller-derived session in the shared store."""
    session = getattr(ex, "session", None)
    if session is not None:
        return session
    access = getattr(ex, "sessions", None)
    handle = caller(ex)
    if access is None or handle is None:
        return None
    found = await access.use(handle)
    if found is None:
        await access.store.create(scoped(handle), {DATA_KEY: {}}, ttl_seconds=access.ttl_seconds)
        found = await access.use(handle)
    return found


async def granted(ex: Exchange, action: str, target: str) -> bool:
    """Whether this caller already allowed this, and said not to ask again."""
    session = await remembering(ex)
    if session is None:
        return False
    allowed = session.get(CONSENT_KEY) or []
    return scope(action, target) in allowed or scope(action, "*") in allowed


async def keep(ex: Exchange, action: str, target: str, remember: str) -> None:
    """Write down what may now be done without asking again."""
    if remember not in ("this", "any"):
        return
    session = await remembering(ex)
    if session is None:
        return
    wanted = scope(action, target if remember == "this" else "*")
    allowed = list(session.get(CONSENT_KEY) or [])
    if wanted not in allowed:
        await session.set(CONSENT_KEY, [*allowed, wanted])


def refusal(ex: Exchange, key: str = "confirm") -> str:
    """Distinguish an explicit decline from a cancelled confirmation."""
    action = ex.action(key)
    return action.value if action is not None else "unanswered"


async def confirm(ex: Exchange, policy: Policy, action: str, target: str, text: str) -> bool:
    """Apply policy and remembered grants before asking. An accepted form with confirmed=false is
    still a refusal.
    """
    if not policy.asks(action):
        return True
    if await granted(ex, action, target):
        return True
    keepable = await remembering(ex) is not None
    answer = await ex.ask("confirm", question(text, keepable=keepable))
    if not (answer.accepted and answer.get("confirmed")):
        return False
    await keep(ex, action, target, str(answer.get("remember") or "once"))
    return True


__all__ = [
    "CONFIRM",
    "CONSENT_KEY",
    "DESTROYS",
    "LEVELS",
    "Policy",
    "READING",
    "REMEMBER",
    "caller",
    "confirm",
    "granted",
    "keep",
    "only_reads",
    "question",
    "refusal",
    "remembering",
]
