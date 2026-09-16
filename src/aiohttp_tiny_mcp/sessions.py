"""Session storage for protocol state and application values. See docs/guide/sessions.md.

Legacy sessions retain the negotiated revision, capabilities, log level, and subscriptions.
Revisions without a handshake use explicit application handles.

Multi-worker deployments need a shared backend; MemorySessionStore is process-local.
"""

from __future__ import annotations

import asyncio
import random
import secrets
import time
from abc import abstractmethod
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from .namespaces import scoped

SESSION_HEADER = "Mcp-Session-Id"
OWNER_KEY = "owner"
DEFAULT_TTL_SECONDS = 3600


def new_session_id() -> str:
    return secrets.token_urlsafe(24)


@dataclass(frozen=True, slots=True)
class SessionRecord:
    """Stored session with a write version, independent of the MCP revision. Pass that version to
    save() to reject concurrent overwrites.
    """

    data: Mapping[str, Any]
    version: int


@runtime_checkable
class SessionStore(Protocol):
    """Application-provided storage, safe across workers.

    create() atomically returns False for an existing id. save() atomically returns False when
    expected_version no longer matches. Store only JSON-serializable values, never requests,
    sockets, queues, or tasks.

    Subclass it to have the methods checked and the missing ones refused, or
    supply any object with these four methods: this is a protocol, so a
    backend that inherits nothing is still a SessionStore.
    """

    @abstractmethod
    async def create(self, session_id: str, data: Mapping[str, Any], *, ttl_seconds: int) -> bool:
        """Create with a TTL. Return False where a live record already holds the id."""

    @abstractmethod
    async def get(self, session_id: str) -> SessionRecord | None:
        """The live record and its version, or None where it is missing or expired."""

    @abstractmethod
    async def save(
        self,
        session_id: str,
        data: Mapping[str, Any],
        *,
        expected_version: int,
        ttl_seconds: int,
    ) -> bool:
        """Replace the data and renew the TTL, or return False where the version moved."""

    @abstractmethod
    async def delete(self, session_id: str) -> None:
        """Forget it."""


class MemorySessionStore(SessionStore):
    """Process-local session store for development, tests, and single-worker deployments."""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self.clock = clock
        self.records: dict[str, tuple[Mapping[str, Any], int, float]] = {}

    def live(self, session_id: str) -> tuple[Mapping[str, Any], int, float] | None:
        entry = self.records.get(session_id)
        if entry is None:
            return None
        if entry[2] <= self.clock():
            del self.records[session_id]
            return None
        return entry

    async def create(self, session_id: str, data: Mapping[str, Any], *, ttl_seconds: int) -> bool:
        if self.live(session_id) is not None:
            return False
        self.records[session_id] = (dict(data), 1, self.clock() + ttl_seconds)
        return True

    async def get(self, session_id: str) -> SessionRecord | None:
        entry = self.live(session_id)
        if entry is None:
            return None
        data, version, _ = entry
        return SessionRecord(data=data, version=version)

    async def save(
        self,
        session_id: str,
        data: Mapping[str, Any],
        *,
        expected_version: int,
        ttl_seconds: int,
    ) -> bool:
        entry = self.live(session_id)
        if entry is None or entry[1] != expected_version:
            return False
        self.records[session_id] = (dict(data), expected_version + 1, self.clock() + ttl_seconds)
        return True

    async def delete(self, session_id: str) -> None:
        self.records.pop(session_id, None)


PROTOCOL_VERSION_KEY = "protocolVersion"
CAPABILITIES_KEY = "capabilities"
LOG_LEVEL_KEY = "logLevel"
DATA_KEY = "data"
SUBSCRIPTIONS_KEY = "subscriptions"
SAVE_ATTEMPTS = 16
RETRY_SECONDS = 0.02


def handshake_data(
    protocol_version: str, capabilities: Mapping[str, Any], owner: str | None = None
) -> dict[str, Any]:
    """Protocol state recorded by a legacy handshake.

    `owner` names the principal that opened it. A session id travels in a
    header and is therefore a credential; without an owner, anyone holding a
    copy is that session.
    """
    recorded: dict[str, Any] = {
        PROTOCOL_VERSION_KEY: protocol_version,
        CAPABILITIES_KEY: dict(capabilities),
    }
    if owner is not None:
        recorded[OWNER_KEY] = owner
    return recorded


def stored_owner(record: SessionRecord | None) -> str | None:
    if record is None:
        return None
    value = record.data.get(OWNER_KEY)
    return value if isinstance(value, str) else None


class SessionExpired(RuntimeError):
    """The session went away while a handler was writing to it."""


class Session:
    """Application session values, read from the request snapshot and written with compare-and-set
    retries.
    """

    def __init__(
        self,
        store: SessionStore,
        session_id: str,
        record: SessionRecord,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
    ) -> None:
        self.store = store
        self.id = session_id
        self.key = scoped(session_id)
        self.record = record
        self.ttl_seconds = ttl_seconds

    @property
    def values(self) -> Mapping[str, Any]:
        return self.slot(DATA_KEY)

    async def pause(self, attempt: int) -> None:
        """Use jitter between retries to avoid collisions; do not delay the first attempt."""
        if attempt:
            await asyncio.sleep(random.uniform(0, RETRY_SECONDS))

    def get(self, key: str, default: Any = None) -> Any:
        return self.values.get(key, default)

    def __contains__(self, key: str) -> bool:
        return key in self.values

    async def set(self, key: str, value: Any) -> None:
        """Store one value. It must survive a JSON round trip."""
        await self.update(lambda values: {**values, key: value})

    async def delete(self, key: str) -> None:
        await self.update(lambda values: {k: v for k, v in values.items() if k != key})

    async def replace(self, values: Mapping[str, Any]) -> None:
        """Replace the entire application-owned mapping."""
        await self.update(lambda _: values)

    async def remember_version(self, version: str) -> None:
        """Store the revision this client negotiated.

        Written where a handshake happens on a transport that keeps no header
        to carry it -- see http_sse.py. The header path writes the same key when it
        opens a session.
        """
        await self.write_value(PROTOCOL_VERSION_KEY, version)

    async def set_log_level(self, level: str) -> None:
        """Persist the requested log severity."""
        await self.write_value(LOG_LEVEL_KEY, level)

    async def write_value(self, key: str, value: Any) -> None:
        """Store one top-level value under compare-and-set.

        A plain value rather than a slot, so it is written the same way
        whichever worker handles the next request.
        """
        for attempt in range(SAVE_ATTEMPTS):
            await self.pause(attempt)
            record = self.record
            saved = await self.store.save(
                self.key,
                {**record.data, key: value},
                expected_version=record.version,
                ttl_seconds=self.ttl_seconds,
            )
            fresh = await self.store.get(self.key)
            if fresh is None:
                raise SessionExpired(f"session {self.id} expired during a write")
            self.record = fresh
            if saved:
                return
        raise SessionExpired(f"session {self.id} kept losing writes to another worker")

    async def update(self, change: Callable[[Mapping[str, Any]], Mapping[str, Any]]) -> None:
        """Apply `change` to the application's values under compare-and-set."""
        await self.update_slot(DATA_KEY, change)

    def slot(self, name: str) -> Mapping[str, Any]:
        value = self.record.data.get(name)
        return value if isinstance(value, Mapping) else {}

    async def update_slot(
        self, name: str, change: Callable[[Mapping[str, Any]], Mapping[str, Any]]
    ) -> None:
        """Apply change under compare-and-set, recomputing it from fresh values on every retry."""
        for attempt in range(SAVE_ATTEMPTS):
            await self.pause(attempt)
            record = self.record
            stored = {**record.data, name: dict(change(self.slot(name)))}
            saved = await self.store.save(
                self.key,
                stored,
                expected_version=record.version,
                ttl_seconds=self.ttl_seconds,
            )
            fresh = await self.store.get(self.key)
            if fresh is None:
                raise SessionExpired(f"session {self.id} expired during a write")
            self.record = fresh
            if saved:
                return
        raise SessionExpired(f"session {self.id} kept losing writes to another worker")


class SessionAccess:
    """Sessions addressed by explicit handles on any revision.

    Return a handle to the caller and accept it as an argument on subsequent requests. Handles
    and legacy session headers resolve to the same Session interface.
    """

    def __init__(self, store: SessionStore, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> None:
        self.store = store
        self.ttl_seconds = ttl_seconds

    async def open(self) -> Session:
        """Create an empty session and return the handle callers must send back."""
        session_id = new_session_id()
        await self.store.create(scoped(session_id), {DATA_KEY: {}}, ttl_seconds=self.ttl_seconds)
        record = await self.store.get(scoped(session_id))
        if record is None:
            raise SessionExpired(f"session {session_id} vanished as it was opened")
        return Session(self.store, session_id, record, self.ttl_seconds)

    async def use(self, handle: str) -> Session | None:
        """Resolve a handle within the current namespace, or return None."""
        record = await self.store.get(scoped(handle))
        if record is None:
            return None
        return Session(self.store, handle, record, self.ttl_seconds)

    async def drop(self, handle: str) -> None:
        await self.store.delete(scoped(handle))


def stored_version(record: SessionRecord | None) -> str | None:
    if record is None:
        return None
    value = record.data.get(PROTOCOL_VERSION_KEY)
    return value if isinstance(value, str) else None


def stored_log_level(record: SessionRecord | None) -> str | None:
    if record is None:
        return None
    value = record.data.get(LOG_LEVEL_KEY)
    return value if isinstance(value, str) else None


def stored_capabilities(record: SessionRecord | None) -> Mapping[str, Any]:
    if record is None:
        return {}
    value = record.data.get(CAPABILITIES_KEY)
    return value if isinstance(value, Mapping) else {}


__all__ = [
    "CAPABILITIES_KEY",
    "OWNER_KEY",
    "DATA_KEY",
    "LOG_LEVEL_KEY",
    "SUBSCRIPTIONS_KEY",
    "DEFAULT_TTL_SECONDS",
    "PROTOCOL_VERSION_KEY",
    "SESSION_HEADER",
    "MemorySessionStore",
    "Session",
    "SessionAccess",
    "SessionExpired",
    "SessionRecord",
    "SessionStore",
    "handshake_data",
    "new_session_id",
    "stored_capabilities",
    "stored_log_level",
    "stored_owner",
    "stored_version",
]
