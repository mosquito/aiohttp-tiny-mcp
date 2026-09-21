"""Store handler state between input rounds; clients carry only an opaque id.

Ids are bound to the original call, scoped by namespace, and expire independently of sessions.
Shared storage allows retries on another node.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import dataclass
from typing import Any

from aiohttp_tiny_mcp.protocol.core import Call, Failure, FailureKind, NeedsInput
from aiohttp_tiny_mcp.storage.namespaces import scoped
from aiohttp_tiny_mcp.storage.sessions import SessionStore

PREFIX = "state/"

DEFAULT_TTL_SECONDS = 600

BINDING_KEY = "binding"
PAYLOAD_KEY = "payload"


def binding_of(call: Call) -> str:
    """Hash normalized call arguments and target so state cannot be reused for a different call."""
    material = json.dumps(
        {
            "operation": call.operation.value,
            "target": call.target,
            "arguments": call.arguments,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(material.encode()).hexdigest()


@dataclass
class RequestStates:
    """State rows, in the store the deployment already supplies."""

    store: SessionStore
    ttl_seconds: int = DEFAULT_TTL_SECONDS

    def key(self, state_id: str) -> str:
        return scoped(PREFIX + state_id)

    async def open(self, call: Call, payload: Any) -> str:
        """Store payload and return its opaque client-visible id."""
        state_id = secrets.token_urlsafe(24)
        created = await self.store.create(
            self.key(state_id),
            {PAYLOAD_KEY: payload, BINDING_KEY: binding_of(call)},
            ttl_seconds=self.ttl_seconds,
        )
        if not created:
            raise RuntimeError("the store refused a fresh request-state id")
        return state_id

    async def read(self, call: Call, state_id: str) -> Any:
        """Read the payload; raise KeyError for missing state or a different call binding."""
        record = await self.store.get(self.key(state_id))
        if record is None or record.data.get(BINDING_KEY) != binding_of(call):
            raise KeyError(state_id)
        return record.data.get(PAYLOAD_KEY)

    async def drop(self, state_id: str) -> None:
        await self.store.delete(self.key(state_id))


async def restore_request_state(call: Call, states: RequestStates) -> Failure | None:
    """Replace the id the client sent with the state the handler left."""
    if call.state is None:
        return None
    if not isinstance(call.state, str):
        return Failure(FailureKind.INVALID_PARAMS, "invalid requestState")
    try:
        call.state = await states.read(call, call.state)
    except KeyError:
        return Failure(FailureKind.INVALID_PARAMS, "invalid requestState")
    return None


async def protect_request_state(
    call: Call, outcome: NeedsInput, states: RequestStates
) -> NeedsInput | Failure:
    """Keep the handler's state and hand the client its id instead."""
    if outcome.state is None:
        return outcome
    try:
        state_id = await states.open(call, outcome.state)
    except (RuntimeError, TypeError) as e:
        return Failure(FailureKind.INTERNAL, f"requestState could not be stored: {e}")
    return NeedsInput(requests=outcome.requests, state=state_id)


__all__ = [
    "DEFAULT_TTL_SECONDS",
    "RequestStates",
    "binding_of",
    "protect_request_state",
    "restore_request_state",
]
