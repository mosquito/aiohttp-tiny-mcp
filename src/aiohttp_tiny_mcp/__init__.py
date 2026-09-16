"""Stateless remote MCP server: aiohttp + pydantic, nothing else."""

from __future__ import annotations

from .client import Client
from .client_base import ClientError, Elicitor
from .core import (
    Answer,
    AnswerAction,
    NeedInput,
    elicit,
    elicit_accept,
    elicit_cancel,
    elicit_decline,
)
from .endpoint import Endpoint
from .exchange import Exchange
from .http_sse import SseEndpoint
from .hub import Hub, MemoryHub
from .models import (
    AudioContent,
    CallToolResult,
    Completion,
    GetPromptResult,
    Hint,
    ImageContent,
    PromptMessage,
    ResourceLink,
    TextContent,
)
from .namespaces import namespace
from .protocol.selection import AdapterSet
from .registry import Registry
from .request_state import RequestStates
from .sessions import MemorySessionStore, SessionRecord, SessionStore
from .sse import SSEResponse
from .stdio import run_stdio, serve_stdio
from .stdio_client import StdioClient

__all__ = [
    "AdapterSet",
    "Answer",
    "AnswerAction",
    "AudioContent",
    "CallToolResult",
    "Client",
    "ClientError",
    "Elicitor",
    "Completion",
    "Endpoint",
    "SSEResponse",
    "Exchange",
    "GetPromptResult",
    "Hint",
    "Hub",
    "ImageContent",
    "MemoryHub",
    "MemorySessionStore",
    "NeedInput",
    "namespace",
    "PromptMessage",
    "Registry",
    "SseEndpoint",
    "RequestStates",
    "ResourceLink",
    "SessionRecord",
    "SessionStore",
    "StdioClient",
    "TextContent",
    "elicit",
    "elicit_accept",
    "elicit_cancel",
    "elicit_decline",
    "run_stdio",
    "serve_stdio",
]
