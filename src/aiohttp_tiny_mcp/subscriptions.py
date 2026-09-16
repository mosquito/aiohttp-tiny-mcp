"""Notification filtering for modern request streams and legacy session subscriptions.

Both read the same hub topic; legacy streams reload subscriptions from the shared session.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from . import hub as hubs
from .models import EmptyResult, ListenParams, Meta
from .sessions import SUBSCRIPTIONS_KEY, Session

if TYPE_CHECKING:
    from .exchange import Exchange

SUBSCRIPTION_ID = "io.modelcontextprotocol/subscriptionId"
LIST_CHANGES = {
    "notifications/tools/list_changed": ("toolsListChanged", "tools"),
    "notifications/prompts/list_changed": ("promptsListChanged", "prompts"),
    "notifications/resources/list_changed": ("resourcesListChanged", "resources"),
}


def tag(payload: Mapping[str, Any], subscription_id: str | int) -> dict[str, Any]:
    params = dict(payload.get("params") or {})
    params["_meta"] = {**params.get("_meta", {}), SUBSCRIPTION_ID: subscription_id}
    return {**payload, "params": params}


def relays(payload: Mapping[str, Any], accepted: Mapping[str, Any]) -> bool:
    """Whether this event is one the client asked for."""
    method = payload.get("method")
    if "id" in payload or payload.get("jsonrpc") != "2.0":
        return False
    if method == "notifications/resources/updated":
        params = payload.get("params")
        if not isinstance(params, Mapping):
            return False
        return params.get("uri") in accepted.get("resourceSubscriptions", [])
    return method in LIST_CHANGES and bool(accepted.get(LIST_CHANGES[method][0]))


async def listen(ex: Exchange) -> EmptyResult:
    assert isinstance(ex.call.params, ListenParams)
    wanted = ex.call.params.notifications
    caps = ex.adapter.capabilities(ex.registry)
    accepted: dict[str, Any] = {}
    requested = wanted.wire()
    for field, capability in LIST_CHANGES.values():
        if requested.get(field) and caps.get(capability, {}).get("listChanged"):
            accepted[field] = True
    if caps.get("resources", {}).get("subscribe"):
        uris = [uri for uri in wanted.resource_subscriptions if ex.registry.match_resource(uri)]
        if uris:
            accepted["resourceSubscriptions"] = list(dict.fromkeys(uris))

    ack = tag(
        {
            "jsonrpc": "2.0",
            "method": "notifications/subscriptions/acknowledged",
            "params": {"notifications": accepted},
        },
        ex.id,
    )
    hub = ex.registry.hub
    if not accepted:
        await ex.emit(ack)
        return EmptyResult(meta=Meta.model_validate({SUBSCRIPTION_ID: ex.id}))

    topic = hubs.topic(hubs.NOTIFICATIONS)
    cursor = await hub.position(topic)
    await ex.emit(ack)
    while not ex.cancelled.is_set():
        messages, cursor = await hub.poll(topic, cursor, timeout=ex.registry.hub_poll_seconds)
        for payload in messages:
            if relays(payload, accepted):
                await ex.emit(tag(payload, ex.id))
    return EmptyResult(meta=Meta.model_validate({SUBSCRIPTION_ID: ex.id}))


URIS = "uris"


def subscribed(session: Session | None) -> list[str]:
    """Resources this client asked to hear about."""
    if session is None:
        return []
    uris = session.slot(SUBSCRIPTIONS_KEY).get(URIS)
    if not isinstance(uris, Mapping):
        return []
    return [uri for uri in uris if isinstance(uri, str)]


async def subscribe(session: Session, uri: str) -> None:
    await session.update_slot(
        SUBSCRIPTIONS_KEY, lambda held: {**held, URIS: {**dict(held.get(URIS) or {}), uri: True}}
    )


async def unsubscribe(session: Session, uri: str) -> None:
    await session.update_slot(
        SUBSCRIPTIONS_KEY,
        lambda held: {
            **held,
            URIS: {key: True for key in dict(held.get(URIS) or {}) if key != uri},
        },
    )


def wanted(capabilities: Mapping[str, Any], session: Session | None) -> dict[str, Any]:
    """Legacy list changes follow advertised capabilities; resource updates follow session
    subscriptions.
    """
    accepted: dict[str, Any] = {}
    for field, capability in LIST_CHANGES.values():
        if capabilities.get(capability, {}).get("listChanged"):
            accepted[field] = True
    if capabilities.get("resources", {}).get("subscribe"):
        uris = subscribed(session)
        if uris:
            accepted["resourceSubscriptions"] = uris
    return accepted
