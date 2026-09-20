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
from .extensions import Extension
from .http_sse import SseEndpoint
from .hub import Hub, MemoryHub
from .models import (
    AudioContent,
    BlobResourceContents,
    CallToolResult,
    Completion,
    EmbeddedResource,
    GetPromptResult,
    Hint,
    ImageContent,
    MethodFilter,
    PromptMessage,
    ResourceLink,
    TextContent,
    TextResourceContents,
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
    "BlobResourceContents",
    "CallToolResult",
    "Client",
    "ClientError",
    "Elicitor",
    "Completion",
    "EmbeddedResource",
    "Endpoint",
    "SSEResponse",
    "Exchange",
    "Extension",
    "GetPromptResult",
    "Hint",
    "Hub",
    "ImageContent",
    "MemoryHub",
    "MemorySessionStore",
    "MethodFilter",
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
    "TextResourceContents",
    "elicit",
    "elicit_accept",
    "elicit_cancel",
    "elicit_decline",
    "run_stdio",
    "serve_stdio",
]
