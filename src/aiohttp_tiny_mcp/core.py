"""Protocol-independent calls, outcomes, failures, and elicitation helpers."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from pydantic import BaseModel

from .models import Implementation, Params


class StringEnum(str, Enum):
    """A string enum that formats as its value on every Python.

    From 3.11 a plain `str, Enum` formats as `Class.MEMBER` where 3.10 gave
    the value, so a name reaching a message or a wire field would change with
    the interpreter.
    """

    def __str__(self) -> str:
        return str(self.value)

    def __format__(self, spec: str) -> str:
        return format(str(self), spec)


class Operation(StringEnum):
    DESCRIBE = "describe"
    HANDSHAKE_COMPLETE = "handshake_complete"
    PING = "ping"
    LIST_TOOLS = "list_tools"
    CALL_TOOL = "call_tool"
    LIST_RESOURCES = "list_resources"
    LIST_RESOURCE_TEMPLATES = "list_resource_templates"
    READ_RESOURCE = "read_resource"
    LIST_PROMPTS = "list_prompts"
    GET_PROMPT = "get_prompt"
    COMPLETE = "complete"
    LISTEN = "listen"
    SUBSCRIBE = "subscribe"
    UNSUBSCRIBE = "unsubscribe"
    SET_LOG_LEVEL = "set_log_level"


LOG_LEVELS: tuple[str, ...] = (
    "debug",
    "info",
    "notice",
    "warning",
    "error",
    "critical",
    "alert",
    "emergency",
)


def logs_at(wanted: str | None, level: str) -> bool:
    """Report the requested severity and above; None disables logging."""
    if wanted is None:
        return False
    try:
        return LOG_LEVELS.index(level) >= LOG_LEVELS.index(wanted)
    except ValueError:
        return True


@dataclass(frozen=True)
class ClientProfile:
    """Client identity, capabilities, and desired log level."""

    info: Implementation
    capabilities: Mapping[str, Any] = field(default_factory=dict)
    log_level: str | None = None


class FailureKind(StringEnum):
    PARSE = "parse"
    MALFORMED = "malformed"
    UNKNOWN_METHOD = "unknown_method"
    UNKNOWN_TARGET = "unknown_target"
    RESOURCE_NOT_FOUND = "resource_not_found"
    INVALID_PARAMS = "invalid_params"
    INVALID_ARGUMENTS = "invalid_arguments"
    HEADER_MISMATCH = "header_mismatch"
    UNSUPPORTED_VERSION = "unsupported_version"
    ORIGIN_REJECTED = "origin_rejected"
    INPUT_UNSUPPORTED = "input_unsupported"
    MISSING_REQUIRED_CAPABILITY = "missing_required_capability"
    INTERNAL = "internal"


InputRequest = Mapping[str, Any]


@dataclass(slots=True)
class Value:
    result: BaseModel


@dataclass(slots=True)
class NeedsInput:
    requests: Mapping[str, InputRequest]
    state: Any = None


@dataclass(slots=True)
class Failure:
    kind: FailureKind
    message: str
    data: Any = None


Outcome = Value | NeedsInput | Failure


@dataclass(frozen=True, slots=True)
class ClientInfo:
    name: str | None = None
    version: str | None = None
    capabilities: Mapping[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class Call:
    operation: Operation
    id: str | int | None
    target: str | None
    arguments: Mapping[str, Any]
    params: Params
    client: ClientInfo
    progress_token: str | int | None = None
    log_level: str | None = None
    answers: Mapping[str, Any] = field(default_factory=dict)
    actions: Mapping[str, AnswerAction] = field(default_factory=dict)
    state: Any = None
    raw: Mapping[str, Any] = field(default_factory=dict)
    is_notification: bool = False


@dataclass(frozen=True, slots=True)
class DecodeFailure:
    """A failed batch item carried alongside valid calls without aborting the batch."""

    id: str | int | None
    failure: Failure
    must_respond: bool = True


def decode_failure_target(body: Mapping[str, Any]) -> tuple[str | int | None, bool]:
    """Return the safe response id and whether an invalid message gets a reply.

    A missing id suppresses a reply only when the object is structurally a
    JSON-RPC notification. Invalid request objects still receive an error with
    a null id. MCP request ids are strings or integers; booleans are not
    integers for this purpose even though ``bool`` subclasses ``int`` in Python.
    """
    if "id" in body:
        value = body["id"]
        usable = isinstance(value, str) or (isinstance(value, int) and not isinstance(value, bool))
        return (value, True) if usable else (None, True)
    params = body.get("params", {})
    is_notification = (
        body.get("jsonrpc") == "2.0"
        and isinstance(body.get("method"), str)
        and isinstance(params, (dict, list))
    )
    return None, not is_notification


VERSION_QUERY_PARAM = "mcp"


@dataclass(frozen=True, slots=True)
class Preamble:
    raw: bytes
    body: Any
    parse_error: bool
    headers: Mapping[str, str]
    is_batch: bool
    method: str | None
    header_version: str | None
    meta_version: str | None
    query_version: str | None
    initialize_version: str | None

    @classmethod
    def of(
        cls,
        raw: bytes,
        headers: Mapping[str, str],
        query: Mapping[str, str] | None = None,
    ) -> Preamble:
        try:
            body = json.loads(raw)
            parse_error = False
        except (json.JSONDecodeError, UnicodeDecodeError):
            body = None
            parse_error = True
        method = body.get("method") if isinstance(body, dict) else None
        params = body.get("params") if isinstance(body, dict) else None
        meta = params.get("_meta") if isinstance(params, dict) else None
        meta_version = (
            meta.get("io.modelcontextprotocol/protocolVersion") if isinstance(meta, dict) else None
        )
        initialize_version = (
            params.get("protocolVersion")
            if method == "initialize" and isinstance(params, dict)
            else None
        )
        return cls(
            raw=raw,
            body=body,
            parse_error=parse_error,
            headers=headers,
            is_batch=isinstance(body, list),
            method=method,
            header_version=headers.get("MCP-Protocol-Version"),
            meta_version=meta_version,
            query_version=(query or {}).get(VERSION_QUERY_PARAM),
            initialize_version=initialize_version,
        )


class Rejected(Exception):
    """A protocol failure that aborts request handling before a result is produced."""

    def __init__(self, failure: Failure) -> None:
        super().__init__(failure.message)
        self.failure = failure


class NeedInput(Exception):
    """Raise from a tool/prompt/resource handler to trigger MRTR."""

    def __init__(self, requests: Mapping[str, InputRequest], state: Any = None) -> None:
        super().__init__("input required")
        self.requests = requests
        self.state = state


def elicit(message: str, schema: dict[str, Any] | None = None) -> InputRequest:
    """Ask the user something. Omit `schema` to ask only for agreement."""
    return {
        "method": "elicitation/create",
        "params": {
            "message": message,
            "requestedSchema": schema
            if schema is not None
            else {"type": "object", "properties": {}},
        },
    }


def elicit_accept(content: Mapping[str, Any]) -> dict[str, Any]:
    return {"action": "accept", "content": dict(content)}


def elicit_decline() -> dict[str, Any]:
    return {"action": "decline"}


def elicit_cancel() -> dict[str, Any]:
    return {"action": "cancel"}


class AnswerAction(StringEnum):
    """What a client did with one input request (`ElicitResult.action`)."""

    ACCEPT = "accept"
    DECLINE = "decline"
    CANCEL = "cancel"


@dataclass(frozen=True, slots=True)
class Answer:
    """Unwrapped reply content and action. Empty content may still mean acceptance."""

    action: AnswerAction
    content: Mapping[str, Any] = field(default_factory=dict)

    @property
    def accepted(self) -> bool:
        return self.action is AnswerAction.ACCEPT

    def get(self, key: str, default: Any = None) -> Any:
        return self.content.get(key, default)

    def __getitem__(self, key: str) -> Any:
        return self.content[key]

    def __contains__(self, key: str) -> bool:
        return key in self.content


def answer_of(reply: Mapping[str, Any]) -> Answer:
    """Convert an ElicitResult envelope into the reply a handler sees."""
    action = reply.get("action")
    unwrapped = unwrap_answers({"": reply})
    try:
        resolved = AnswerAction(action) if isinstance(action, str) else AnswerAction.ACCEPT
    except ValueError:
        resolved = AnswerAction.DECLINE
    return Answer(action=resolved, content=unwrapped.get("", {}))


def answer_actions(raw: Mapping[str, Any] | None) -> dict[str, AnswerAction]:
    """Extract acceptance separately from content: empty forms can still be accepted."""
    if not raw:
        return {}
    actions: dict[str, AnswerAction] = {}
    for key, value in raw.items():
        action = value.get("action") if isinstance(value, dict) else None
        if not isinstance(action, str):
            # A bare answer carries content and no verdict: it is an agreement.
            actions[key] = AnswerAction.ACCEPT
            continue
        try:
            actions[key] = AnswerAction(action)
        except ValueError:
            # An action this revision does not define is not agreement.
            actions[key] = AnswerAction.DECLINE
    return actions


def unwrap_answers(raw: Mapping[str, Any] | None) -> dict[str, Any]:
    """Extract answer content from ElicitResult envelopes. Decline/cancel remain present with empty
    content, so they count as answered.
    """
    if not raw:
        return {}
    answers: dict[str, Any] = {}
    for key, value in raw.items():
        if isinstance(value, dict) and "action" in value:
            accepted = value["action"] == "accept"
            answers[key] = (value.get("content") or {}) if accepted else {}
        else:
            answers[key] = value
    return answers
