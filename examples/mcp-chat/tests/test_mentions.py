from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from mcp_chat.store import Store


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[Store]:
    storage = Store(tmp_path / "chat.sqlite3")
    await storage.open()
    await storage.create_channel("example.com", "alice", "general")
    await storage.create_channel("example.com", "alice", "random")
    try:
        yield storage
    finally:
        await storage.close()


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("@bob", {"bob"}),
        ("Hello @bob, (@ann)! [@two] {@three}: @bob", {"bob", "ann", "two", "three"}),
        ("@Bob @bob @bot-one @bot_one @bot.one", {"Bob", "bob", "bot-one", "bot_one", "bot.one"}),
        ("@alice@example.com", {"alice@example.com"}),
        ("Contact alice@example.com or alice+tag@example.com", set()),
        ("@@bob x@bob _@bob .@bob +@bob -@bob ж@bob", set()),
        ("@bobЖ @bobé @Жbob @-bob @_bob @.bob", set()),
        ("@bob.", {"bob."}),
        ("@bob .", {"bob"}),
        ("@" + "a" * 64, {"a" * 64}),
        ("@" + "a" * 65, set()),
        ("@" + "a" * 65 + "@bob", set()),
        ("`@bob` and code: @ann", {"bob", "ann"}),
    ],
)
def test_mentions_match_complete_user_names(text: str, expected: set[str]) -> None:
    assert Store.mentioned_users(text) == expected


async def test_root_and_reply_mentions_arrive_without_membership(store: Store) -> None:
    root = await store.post_message("example.com", "alice", "random", "@bob please review")
    reply = await store.post_message("example.com", "alice", "random", "Follow up @bob", root["id"])
    await store.post_message("example.com", "alice", "random", "Unrelated")
    assert await store.inbox("example.com", "bob") == [root, reply]
    assert await store.list_subscriptions("example.com", "bob") == []
    channel = next(
        row for row in await store.list_channels("example.com", "bob") if row["name"] == "random"
    )
    assert channel["subscribed"] is False
    assert channel["unread"] == 2
    result = await store.mark_read("example.com", "bob", "random", reply["id"])
    assert result["subscribed"] is False
    assert result["last_read_id"] == reply["id"]
    assert await store.inbox("example.com", "bob") == []
    repeated = await store.mark_read("example.com", "bob", "random", root["id"])
    assert repeated["last_read_id"] == reply["id"]
    assert await store.list_subscriptions("example.com", "bob") == []


async def test_subscribed_and_repeated_mentions_are_not_duplicated(store: Store) -> None:
    await store.subscribe("example.com", "bob", "general")
    message = await store.post_message("example.com", "alice", "general", "@bob @bob @bob")
    own = await store.post_message("example.com", "bob", "general", "@bob my own message")
    assert await store.inbox("example.com", "bob") == [message]
    channel = next(
        row for row in await store.list_channels("example.com", "bob") if row["name"] == "general"
    )
    assert channel["unread"] == 1
    await store.mark_read("example.com", "bob", "general", own["id"])
    await store.unsubscribe("example.com", "bob", "general")
    assert await store.inbox("example.com", "bob") == []


async def test_mentions_survive_join_leave_and_restart(store: Store) -> None:
    first = await store.post_message("example.com", "alice", "random", "@bob before joining")
    await store.subscribe("example.com", "bob", "random")
    assert await store.inbox("example.com", "bob") == [first]
    second = await store.post_message("example.com", "alice", "random", "@bob after joining")
    await store.post_message("example.com", "alice", "random", "Ordinary member message")
    await store.unsubscribe("example.com", "bob", "random")
    assert await store.inbox("example.com", "bob") == [first, second]
    await store.mark_read("example.com", "bob", "random", first["id"])
    await store.close()
    await store.open()
    assert await store.inbox("example.com", "bob") == [second]
    await store.subscribe("example.com", "bob", "random")
    assert await store.inbox("example.com", "bob") == [second]
    await store.mark_read("example.com", "bob", "random", second["id"])
    await store.unsubscribe("example.com", "bob", "random")
    await store.close()
    await store.open()
    assert await store.inbox("example.com", "bob") == []


async def test_mentions_mix_with_subscriptions_in_paginated_id_order(store: Store) -> None:
    await store.subscribe("example.com", "bob", "general")
    first = await store.post_message("example.com", "alice", "general", "Member message")
    second = await store.post_message("example.com", "alice", "random", "@bob mention")
    await store.post_message("example.com", "alice", "random", "Not for bob")
    third = await store.post_message("example.com", "alice", "general", "@bob repeated @bob")
    fourth = await store.post_message("example.com", "alice", "random", "@bob reply", second["id"])
    page = await store.inbox("example.com", "bob", limit=2)
    assert page == [first, second]
    page = await store.inbox("example.com", "bob", after=page[-1]["id"], limit=2)
    assert page == [third, fourth]
    assert await store.inbox("example.com", "bob", after=page[-1]["id"], limit=2) == []
    await store.mark_read("example.com", "bob", "random", fourth["id"])
    assert await store.inbox("example.com", "bob") == [first, third]


async def test_mentions_do_not_cross_orgs_or_private_conversations(store: Store) -> None:
    await store.create_channel("other.example", "alice", "random")
    foreign = await store.post_message("other.example", "alice", "random", "@bob foreign")
    private = await store.send_dm("example.com", "alice", "carol", "@bob private")
    visible = await store.post_message("example.com", "alice", "random", "@bob public")
    assert await store.inbox("example.com", "bob") == [visible]
    assert await store.inbox("other.example", "bob") == [foreign]
    assert await store.list_dms("example.com", "bob") == []
    with pytest.raises(ValueError):
        await store.history("example.com", private["channel"], user="bob")
    with pytest.raises(ValueError):
        await store.mark_read("example.com", "bob", private["channel"], private["id"])
    with pytest.raises(ValueError):
        await store.mark_read("example.com", "bob", "random", foreign["id"])
    assert await store.inbox("example.com", "bob") == [visible]
    assert await store.inbox("example.com", "carol") == [private]


async def test_nonmembers_can_acknowledge_only_their_delivered_mentions(store: Store) -> None:
    first = await store.post_message("example.com", "alice", "random", "@bob first")
    unrelated = await store.post_message("example.com", "alice", "random", "@carol second")
    with pytest.raises(ValueError, match="received mention"):
        await store.mark_read("example.com", "bob", "random", unrelated["id"])
    with pytest.raises(ValueError, match="this channel"):
        await store.mark_read("example.com", "bob", "general", first["id"])
    assert await store.inbox("example.com", "bob") == [first]
    assert await store.list_subscriptions("example.com", "bob") == []


async def test_notification_recipients_include_only_current_public_mentions(store: Store) -> None:
    await store.subscribe("example.com", "member", "random")
    first = await store.post_message("example.com", "alice", "random", "@bob @bob")
    second = await store.post_message("example.com", "alice", "random", "@carol")
    assert await store.notification_recipients("example.com", "alice", "random", first["id"]) == {
        "kind": "channel",
        "users": ["alice", "bob", "member"],
    }
    assert await store.notification_recipients("example.com", "alice", "random", second["id"]) == {
        "kind": "channel",
        "users": ["alice", "carol", "member"],
    }
    assert await store.notification_recipients("example.com", "alice", "random") == {
        "kind": "channel",
        "users": ["alice", "member"],
    }
    private = await store.send_dm("example.com", "alice", "carol", "@bob")
    assert await store.notification_recipients(
        "example.com", "alice", private["channel"], private["id"]
    ) == {"kind": "dm", "users": ["alice", "carol"]}
