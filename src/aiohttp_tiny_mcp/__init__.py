"""Stateless remote MCP server: aiohttp + pydantic, nothing else."""

from __future__ import annotations

from .auth import (
    Authentication,
    Authorization,
    BasicAuth,
    Principal,
    StaticBasicAuth,
    StaticVerifier,
    TokenVerifier,
    Unauthorized,
    principal_from_claims,
)
from .client.base import ClientError, Elicitor
from .client.http import Client
from .client.stdio import StdioClient
from .extensions import Extension
from .protocol.core import (
    Answer,
    AnswerAction,
    NeedInput,
    elicit,
    elicit_accept,
    elicit_cancel,
    elicit_decline,
)
from .protocol.models import (
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
from .protocol.selection import AdapterSet
from .server.exchange import Exchange
from .server.http import Endpoint
from .server.registry import Registry
from .server.request_state import RequestStates
from .server.sse import SseEndpoint
from .server.stdio import run_stdio, serve_stdio
from .storage.hub import Hub, MemoryHub
from .storage.namespaces import namespace
from .storage.sessions import MemorySessionStore, SessionRecord, SessionStore
from .transport.sse import SSEResponse

__all__ = [
    "AdapterSet",
    "Answer",
    "AnswerAction",
    "AudioContent",
    "Authentication",
    "Authorization",
    "BasicAuth",
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
    "Principal",
    "Registry",
    "SseEndpoint",
    "RequestStates",
    "ResourceLink",
    "SessionRecord",
    "SessionStore",
    "StdioClient",
    "StaticBasicAuth",
    "StaticVerifier",
    "TokenVerifier",
    "TextContent",
    "TextResourceContents",
    "Unauthorized",
    "elicit",
    "elicit_accept",
    "elicit_cancel",
    "elicit_decline",
    "principal_from_claims",
    "run_stdio",
    "serve_stdio",
]
