"""Redis `SessionStore` and `Hub` backends."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Any

from .hub import START, Cursor, Event, Hub
from .sessions import SessionRecord, SessionStore

try:
    from redis.asyncio import Redis
except ImportError as absent:  # pragma: no cover - depends on what is installed
    raise ImportError(
        'aiohttp_tiny_mcp.storage.redis needs redis. Install "aiohttp-tiny-mcp[redis]".'
    ) from absent

BEGINNING = "0-0"

CREATE = """
if redis.call('EXISTS', KEYS[1]) == 1 then return 0 end
redis.call('HSET', KEYS[1], 'data', ARGV[1], 'version', 1)
redis.call('EXPIRE', KEYS[1], ARGV[2])
return 1
"""

SAVE = """
local version = redis.call('HGET', KEYS[1], 'version')
if not version or tonumber(version) ~= tonumber(ARGV[2]) then return 0 end
redis.call('HSET', KEYS[1], 'data', ARGV[1], 'version', ARGV[3])
redis.call('EXPIRE', KEYS[1], ARGV[4])
return 1
"""


def text(value: Any) -> str:
    """Redis answers in bytes, or in strings where the client decodes them."""
    return value.decode() if isinstance(value, bytes) else str(value)


class RedisStorage:
    """A Redis client shared by the store and hub."""

    prefix: str = "mcp"

    def __init__(
        self,
        client: Redis,
        *,
        prefix: str | None = None,
        owned: bool = False,
    ) -> None:
        self.client = client
        self.owned = owned
        if prefix is not None:
            self.prefix = prefix

    @classmethod
    def from_url(cls, url: str, **settings: Any) -> RedisStorage:
        """Build an owned Redis client from a URL."""
        return cls(Redis.from_url(url), owned=True, **settings)

    async def open(self) -> Redis:
        return self.client

    async def close(self) -> None:
        """Close an owned client."""
        if self.owned:
            await self.client.aclose()

    async def cleanup_ctx(self, app: Any) -> AsyncIterator[None]:
        """Manage an owned client for an aiohttp app."""
        await self.open()
        try:
            yield
        finally:
            await self.close()


class RedisSessionStore(SessionStore):
    """`SessionStore` on one key per session, with version-checked writes."""

    def __init__(self, storage: RedisStorage, *, prefix: str | None = None) -> None:
        self.storage = storage
        self.prefix = prefix if prefix is not None else storage.prefix
        self.scripts: dict[str, Any] = {}

    async def script(self, source: str) -> Any:
        """Register a script once per client, and keep it by its source."""
        if source not in self.scripts:
            client = await self.storage.open()
            self.scripts[source] = client.register_script(source)
        return self.scripts[source]

    def key(self, session_id: str) -> str:
        return f"{self.prefix}:session:{session_id}"

    async def create(self, session_id: str, data: Mapping[str, Any], *, ttl_seconds: int) -> bool:
        create = await self.script(CREATE)
        written: Any = await create(
            keys=[self.key(session_id)], args=[json.dumps(data), ttl_seconds]
        )
        return bool(written)

    async def get(self, session_id: str) -> SessionRecord | None:
        client = await self.storage.open()
        held: Any = await client.hmget(self.key(session_id), ["data", "version"])
        data, version = held
        if data is None or version is None:
            return None
        return SessionRecord(data=json.loads(data), version=int(version))

    async def save(
        self,
        session_id: str,
        data: Mapping[str, Any],
        *,
        expected_version: int,
        ttl_seconds: int,
    ) -> bool:
        save = await self.script(SAVE)
        written: Any = await save(
            keys=[self.key(session_id)],
            args=[json.dumps(data), expected_version, expected_version + 1, ttl_seconds],
        )
        return bool(written)

    async def touch(self, session_id: str, *, ttl_seconds: int) -> bool:
        """EXPIRE answers 1 only for a key that exists, so a lost session reads False."""
        client = await self.storage.open()
        renewed: Any = await client.expire(self.key(session_id), ttl_seconds)
        return bool(renewed)

    async def delete(self, session_id: str) -> None:
        client = await self.storage.open()
        await client.delete(self.key(session_id))


class RedisHub(Hub):
    """A Redis stream per topic."""

    keep: int = 1000

    ttl_seconds: int = 3600

    def __init__(
        self,
        storage: RedisStorage,
        *,
        prefix: str | None = None,
        keep: int | None = None,
        ttl_seconds: int | None = None,
    ) -> None:
        self.storage = storage
        self.prefix = prefix if prefix is not None else storage.prefix
        if keep is not None:
            self.keep = keep
        if ttl_seconds is not None:
            self.ttl_seconds = ttl_seconds

    def key(self, topic: str) -> str:
        return f"{self.prefix}:topic:{topic}"

    async def publish(self, topic: str, message: Mapping[str, Any]) -> Cursor:
        client = await self.storage.open()
        key = self.key(topic)
        added: Any = await client.xadd(
            key, {"message": json.dumps(message)}, maxlen=self.keep, approximate=True
        )
        await client.expire(key, self.ttl_seconds)
        return text(added)

    async def position(self, topic: str) -> Cursor:
        client = await self.storage.open()
        last: Any = await client.xrevrange(self.key(topic), count=1)
        return text(last[0][0]) if last else START

    async def poll(self, topic: str, cursor: Cursor, *, timeout: float) -> Sequence[Event]:
        """The stream id is the event id. Redis refuses a malformed one."""
        from redis.exceptions import ResponseError

        client = await self.storage.open()
        try:
            found: Any = await client.xread(
                {self.key(topic): cursor or BEGINNING}, block=max(1, int(timeout * 1000))
            )
        except ResponseError as e:
            raise ValueError(f"not a stream id: {cursor!r}") from e
        if not found:
            return []
        return [
            Event(text(entry_id), json.loads(next(iter(fields.values()))))
            for entry_id, fields in found[0][1]
        ]

    async def delete(self, topic: str) -> None:
        client = await self.storage.open()
        await client.delete(self.key(topic))


__all__ = [
    "BEGINNING",
    "CREATE",
    "SAVE",
    "RedisHub",
    "RedisSessionStore",
    "RedisStorage",
]
