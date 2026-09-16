"""The 2024-11-05 adapter."""

from __future__ import annotations

from typing import Any, ClassVar

from aiohttp_tiny_mcp.core import Call
from aiohttp_tiny_mcp.models import AudioContent, CallToolResult, ToolDef
from aiohttp_tiny_mcp.specs import ToolSpec

from .v2025_03_26 import Adapter2025_03_26


class Adapter2024_11_05(Adapter2025_03_26):  # noqa: N801 -- revision date, greppable against the spec
    version: ClassVar[str] = "2024-11-05"
    allows_batch: ClassVar[bool] = False
    progress_message: ClassVar[bool] = False

    def describe_tool(self, spec: ToolSpec) -> ToolDef | None:
        definition = super().describe_tool(spec)
        if definition is None:
            return None
        return definition.model_copy(update={"annotations": None})

    def encode_value(self, call: Call, registry: Any, result: Any) -> dict[str, Any]:
        if isinstance(result, CallToolResult):
            kept = [block for block in result.content if not isinstance(block, AudioContent)]
            if len(kept) != len(result.content):
                result = result.model_copy(update={"content": kept})
        return super().encode_value(call, registry, result)
