"""Notification filtering for modern request streams and legacy session subscriptions.

Both read the same hub topic; legacy streams reload subscriptions from the shared session.
Extension broadcasts travel the same topic: a modern stream names the methods it wants, a
legacy stream gets every declared one.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from . import hub as hubs
from .models import EmptyResult, ListenParams, Meta, MethodFilter
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


def relays(
    payload: Mapping[str, Any],
    accepted: Mapping[str, Any],
    fields: Mapping[str, str | None] = MappingProxyType({}),
) -> bool:
    """Whether this event is one the client asked for.

    `fields` maps a multicast method to the params field that carries its
    topic; an accepted entry with `topics` matches only when that field holds
    one of them.
    """
    method = payload.get("method")
    if "id" in payload or payload.get("jsonrpc") != "2.0":
        return False
    if method == "notifications/resources/updated":
        params = payload.get("params")
        if not isinstance(params, Mapping):
            return False
        return params.get("uri") in accepted.get("resourceSubscriptions", [])
    if method in LIST_CHANGES:
        return bool(accepted.get(LIST_CHANGES[method][0]))
    for entry in accepted.get("methods", ()):
        if isinstance(entry, str):
            if entry == method:
                return True
            continue
        if entry.get("method") != method:
            continue
        field = fields.get(method)
        params = payload.get("params")
        if field is None or not isinstance(params, Mapping):
            return False
        return params.get(field) in entry.get("topics", ())
    return False


def broadcasts_for(ex: Exchange) -> dict[str, str | None]:
    """The broadcasts this revision may hear, those of the extensions it can see, each with
    its topic field.
    """
    found: dict[str, str | None] = {}
    for spec in ex.registry.extensions.values():
        if spec.min_revision <= ex.adapter.version:
            found.update(spec.notifications)
    return found


def merged(
    requested: Iterable[str | MethodFilter], declared: Mapping[str, str | None]
) -> list[str | dict[str, Any]]:
    """The entries a listen request keeps, in first-seen order, one per method.

    A bare name asks for every event of the method, so it wins over any
    filter for the same method. Filters on one method merge their topics. A
    filter on a method declared without a topic field has nothing to match
    on and becomes the bare name. Undeclared methods are dropped.
    """
    kept: dict[str, list[str] | None] = {}
    for entry in requested:
        name = entry if isinstance(entry, str) else entry.method
        if name not in declared:
            continue
        topics = None if isinstance(entry, str) or declared[name] is None else entry.topics
        held = kept.get(name, [])
        if topics is None or held is None:
            kept[name] = None
        else:
            kept[name] = list(dict.fromkeys([*held, *topics]))
    return [
        name if topics is None else {"method": name, "topics": topics}
        for name, topics in kept.items()
    ]


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
    declared = broadcasts_for(ex)
    methods = merged(wanted.methods, declared)
    if methods:
        accepted["methods"] = methods

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

    events = await hub.subscribe(hubs.topic(hubs.NOTIFICATIONS), wait=ex.registry.hub_poll_seconds)
    await ex.emit(ack)
    while not ex.cancelled.is_set():
        for event in await events.poll():
            if relays(event.message, accepted, declared):
                await ex.emit(tag(event.message, ex.id))
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


def wanted(
    capabilities: Mapping[str, Any], session: Session | None, broadcasts: Iterable[str] = ()
) -> dict[str, Any]:
    """Legacy list changes follow advertised capabilities; resource updates follow session
    subscriptions; every declared broadcast is relayed, as a legacy stream cannot choose.
    """
    accepted: dict[str, Any] = {}
    for field, capability in LIST_CHANGES.values():
        if capabilities.get(capability, {}).get("listChanged"):
            accepted[field] = True
    if capabilities.get("resources", {}).get("subscribe"):
        uris = subscribed(session)
        if uris:
            accepted["resourceSubscriptions"] = uris
    methods = sorted(broadcasts)
    if methods:
        accepted["methods"] = methods
    return accepted
