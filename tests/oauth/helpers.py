from __future__ import annotations

from pydantic import BaseModel

from aiohttp_tiny_mcp.protocol.selection import AdapterSet


class Nothing(BaseModel):
    pass


ADAPTER = AdapterSet.default().by_version["2026-07-28"]


ISSUER = "https://mcp.example/oauth"


RESOURCE = "https://mcp.example/mcp"
