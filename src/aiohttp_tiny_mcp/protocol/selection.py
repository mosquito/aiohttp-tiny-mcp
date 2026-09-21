"""AdapterSet: selects exactly one Adapter per request (docs/reference/selection.md)."""

from __future__ import annotations

from collections.abc import Sequence
from typing import TypeVar

from aiohttp_tiny_mcp.protocol.adapter import Adapter
from aiohttp_tiny_mcp.protocol.core import Failure, FailureKind, Preamble, Rejected

from .v2024_11_05 import Adapter2024_11_05
from .v2025_03_26 import Adapter2025_03_26
from .v2025_06_18 import Adapter2025_06_18
from .v2025_11_25 import Adapter2025_11_25
from .v2026_07_28 import Adapter2026_07_28

ASSUMED_VERSION = "2025-03-26"

STABLE_REVISION = "2025-11-25"

T = TypeVar("T", bound="AdapterSet")


class AdapterSet:
    def __init__(self, adapters: Sequence[Adapter], *, fallback: Adapter | None = None) -> None:
        self.adapters = tuple(adapters)
        self.by_version: dict[str, Adapter] = {}
        for adapter in self.adapters:
            self.by_version[adapter.version] = adapter
            for old in adapter.supersedes:
                self.by_version[old] = adapter
        self.fallback_adapter = fallback or self.adapters[0]
        for adapter in self.adapters:
            adapter.bind(self.versions)

    @classmethod
    def default(cls: type[T]) -> T:
        return cls(
            [
                Adapter2026_07_28(),
                Adapter2025_11_25(),
                Adapter2025_06_18(),
                Adapter2025_03_26(),
                Adapter2024_11_05(),
            ]
        )

    @property
    def versions(self) -> tuple[str, ...]:
        return tuple(sorted({a.version for a in self.adapters}, reverse=True))

    def select(self, pre: Preamble, session_version: str | None = None) -> Adapter:
        if pre.parse_error:
            return self.fallback()
        if pre.meta_version is not None and not isinstance(pre.meta_version, str):
            raise Rejected(
                Failure(FailureKind.MALFORMED, "protocol version in _meta must be a string")
            )
        if pre.query_version is not None:
            return self.resolve_version(pre.query_version)
        if (
            pre.header_version is not None
            and pre.meta_version is not None
            and pre.header_version != pre.meta_version
        ):
            raise Rejected(
                Failure(FailureKind.HEADER_MISMATCH, "MCP-Protocol-Version does not match _meta")
            )
        if pre.meta_version is not None:
            return self.resolve_version(pre.meta_version)
        if pre.header_version is not None:
            return self.resolve_version(pre.header_version)
        if pre.method == "initialize":
            return self.by_version.get(STABLE_REVISION, self.fallback())
        if session_version is not None:
            return self.resolve_version(session_version)
        return self.by_version.get(ASSUMED_VERSION, self.fallback())

    def resolve_version(self, version: str) -> Adapter:
        adapter = self.by_version.get(version)
        if adapter is None:
            raise Rejected(
                Failure(
                    FailureKind.UNSUPPORTED_VERSION,
                    f"unsupported protocol version: {version}",
                    data={"supported": list(self.versions), "requested": version},
                )
            )
        return adapter

    def fallback(self) -> Adapter:
        return self.fallback_adapter
