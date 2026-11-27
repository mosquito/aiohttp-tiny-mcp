"""Freshness hints a server states once, for every result of a cacheable method.

`2026-07-28` requires `ttlMs` and `cacheScope` on each cacheable result (SEP-2549). A
resource declares its own with `cache_ttl` and `cache_scope`. Everything else takes
the default `Registry(cache_hints=...)` sets, and `ttlMs: 0` with
`cacheScope: "private"` where nothing is set, which tells a client to keep nothing.

The set of cacheable methods is derived from the result each operation answers with, so
it follows the models rather than a list kept by hand.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal

from aiohttp_tiny_mcp.protocol.models import CacheableResult, cache_ttl_milliseconds
from aiohttp_tiny_mcp.protocol.v2026_07_28 import METHOD_NAMES, RESULT_MODELS

CacheScope = Literal["public", "private"]

CACHEABLE_METHODS: frozenset[str] = frozenset(
    METHOD_NAMES[operation]
    for operation, result in RESULT_MODELS.items()
    if issubclass(result, CacheableResult)
)
"""The methods whose results carry freshness fields, by their `2026-07-28` names."""


@dataclass(frozen=True, slots=True)
class CacheHint:
    """How long a result stays fresh, and who may keep it.

    `cache_ttl` is a number of seconds, where `0` means immediately stale. `scope` is
    `"private"` for one client and `"public"` for a shared cache. Leave a field unset to
    let a handler or a resource declaration decide it.
    """

    cache_ttl: int | float | None = None
    scope: CacheScope | None = None

    def __post_init__(self) -> None:
        if self.cache_ttl is not None:
            cache_ttl_milliseconds(self.cache_ttl)
        if self.scope is not None and self.scope not in ("public", "private"):
            raise ValueError(f"cache scope must be 'public' or 'private': {self.scope!r}")


def checked_cache_hints(hints: Mapping[str, CacheHint] | None) -> Mapping[str, CacheHint]:
    """Return the hints to keep, or refuse a method that has nowhere to carry them.

    A method outside the revision's cacheable set is a mistake worth reporting at
    construction. A request would otherwise answer a result with no place for the fields,
    and the hint would be silently lost.
    """
    if not hints:
        return MappingProxyType({})
    for method, hint in hints.items():
        if not isinstance(hint, CacheHint):
            raise TypeError(f"cache hint for {method!r} must be a CacheHint, not {type(hint)}")
        if method not in CACHEABLE_METHODS:
            raise ValueError(
                f"{method!r} has no cacheable result. Use one of: "
                f"{', '.join(sorted(CACHEABLE_METHODS))}."
            )
    return MappingProxyType(dict(hints))


@dataclass(slots=True)
class Cache:
    """The freshness lifetime in seconds for the resource read in progress."""

    ttl: int | float = 0

    def set_ttl(self, seconds: int | float) -> None:
        """Set the lifetime of this result in seconds."""
        self.ttl = seconds


__all__ = ["CACHEABLE_METHODS", "Cache", "CacheHint", "CacheScope", "checked_cache_hints"]
