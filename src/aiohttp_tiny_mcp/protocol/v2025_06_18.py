"""2025-06-18: the revision identifier, and no icons.

Icons and `websiteUrl` arrived in 2025-11-25. Everything else is the same as that
revision. The two older adapters extend this one, so they inherit the stripping.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Any, ClassVar

from aiohttp_tiny_mcp.protocol.core import Call
from aiohttp_tiny_mcp.protocol.models import ToolDef
from aiohttp_tiny_mcp.server.specs import ToolSpec

from .adapter import RegistryProtocol
from .v2025_11_25 import Adapter2025_11_25, InitializeResult


class Adapter2025_06_18(Adapter2025_11_25):  # noqa: N801 -- revision date, greppable against the spec
    version: ClassVar[str] = "2025-06-18"

    #: Read by `model_copy`, so it is shared and must stay read-only.
    WITHOUT_ICONS: ClassVar[Mapping[str, Any]] = MappingProxyType({"icons": None})

    def build_tool(self, spec: ToolSpec) -> ToolDef | None:
        definition = super().build_tool(spec)
        return None if definition is None else definition.model_copy(update=self.WITHOUT_ICONS)

    def describe_resource(self, spec: Any) -> Any:
        definition = super().describe_resource(spec)
        return None if definition is None else definition.model_copy(update=self.WITHOUT_ICONS)

    def describe_prompt(self, spec: Any) -> Any:
        definition = super().describe_prompt(spec)
        return None if definition is None else definition.model_copy(update=self.WITHOUT_ICONS)

    def describe_server(self, registry: RegistryProtocol, call: Call) -> InitializeResult:
        described = super().describe_server(registry, call)
        info = described.server_info.model_copy(update={"icons": None, "website_url": None})
        return described.model_copy(update={"server_info": info})
