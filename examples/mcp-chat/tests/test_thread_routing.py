from collections.abc import AsyncIterator
from pathlib import Path

import aiosqlite
import pytest

from mcp_chat.store import SCHEMA, Store


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[Store]:
    value = Store(tmp_path / "routing.sqlite3")
    await value.open()
    await value.create_channel("example.com", "alice", "general")
    for user in ("bob", "carol", "observer"):
        await value.subscribe("example.com", user, "general")
    try:
        yield value
    finally:
        await value.close()


async def unread_ids(store: Store, user: str) -> list[int]:
    return [message["id"] for message in await store.inbox("example.com", user)]


async def test_thread_only_replies_target_authors_and_explicit_mentions(store: Store) -> None:
    root = await store.post_message("example.com", "alice", "general", "Topic")
    reply = await store.post_message("example.com", "bob", "general", "@outsider", root["id"])
    assert await unread_ids(store, "alice") == [reply["id"]]
    assert await unread_ids(store, "observer") == [root["id"]]
    assert await unread_ids(store, "outsider") == [reply["id"]]
    assert await unread_ids(store, "bob") == [root["id"]]
    followup = await store.post_message("example.com", "carol", "general", "Follow up", root["id"])
    assert await unread_ids(store, "alice") == [reply["id"], followup["id"]]
    assert await unread_ids(store, "bob") == [root["id"], followup["id"]]
    assert await unread_ids(store, "outsider") == [reply["id"]]
    assert await unread_ids(store, "observer") == [root["id"]]
    history = await store.history("example.com", "general")
    assert [message["id"] for message in history] == [root["id"]]
    assert history[0]["reply_count"] == 2
    assert history[0]["send_to_channel"] is False
    thread = await store.thread("example.com", root["id"])
    assert [message["id"] for message in thread] == [root["id"], reply["id"], followup["id"]]
    assert [message["reply_count"] for message in thread] == [2, 0, 0]
    assert [message["id"] for message in await store.search("example.com", "Follow")] == [
        followup["id"]
    ]


async def test_broadcast_reply_keeps_one_id_and_delivers_once(store: Store) -> None:
    root = await store.post_message("example.com", "alice", "general", "Topic")
    reply = await store.post_message(
        "example.com", "bob", "general", "@observer @observer", root["id"], True
    )
    assert reply["send_to_channel"] is True
    assert await unread_ids(store, "observer") == [root["id"], reply["id"]]
    assert [message["id"] for message in await store.history("example.com", "general")] == [
        root["id"],
        reply["id"],
    ]
    assert [message["id"] for message in await store.thread("example.com", root["id"])] == [
        root["id"],
        reply["id"],
    ]
    with pytest.raises(ValueError, match="requires a thread reply"):
        await store.post_message("example.com", "alice", "general", "Invalid", send_to_channel=True)
    with pytest.raises(ValueError, match="requires a thread reply"):
        await store.send_dm("example.com", "alice", "new-peer", "Invalid", send_to_channel=True)
    assert await store.list_dms("example.com", "alice") == []


async def test_authors_remain_thread_recipients_after_leaving(store: Store) -> None:
    root = await store.post_message("example.com", "alice", "general", "Topic")
    first = await store.post_message("example.com", "bob", "general", "Answer", root["id"])
    await store.unsubscribe("example.com", "alice", "general")
    await store.unsubscribe("example.com", "bob", "general")
    await store.unsubscribe("example.com", "observer", "general")
    second = await store.post_message("example.com", "carol", "general", "Follow up", root["id"])
    assert await unread_ids(store, "alice") == [first["id"], second["id"]]
    assert await unread_ids(store, "bob") == [second["id"]]
    assert await unread_ids(store, "observer") == []
    assert await store.list_subscriptions("example.com", "alice") == []
    with pytest.raises(ValueError, match="Subscribe"):
        await store.post_message("example.com", "alice", "general", "Not allowed", root["id"])
    await store.close()
    await store.open()
    assert await unread_ids(store, "alice") == [first["id"], second["id"]]


async def test_exact_ack_does_not_clear_hidden_replies_and_validates_batch(store: Store) -> None:
    root = await store.post_message("example.com", "alice", "general", "Root")
    hidden = await store.post_message("example.com", "bob", "general", "Reply", root["id"])
    later = await store.post_message("example.com", "carol", "general", "Later root")
    await store.mark_read("example.com", "alice", "general", [root["id"], later["id"]])
    assert await unread_ids(store, "alice") == [hidden["id"]]
    await store.create_channel("example.com", "alice", "other")
    other = await store.post_message("example.com", "alice", "other", "Other")
    with pytest.raises(ValueError, match="this channel"):
        await store.mark_read("example.com", "alice", "general", [hidden["id"], other["id"]])
    assert await unread_ids(store, "alice") == [hidden["id"]]
    result = await store.mark_read("example.com", "alice", "general", [hidden["id"], hidden["id"]])
    assert result["message_ids"] == [hidden["id"]]
    await store.mark_read("example.com", "alice", "general", hidden["id"])
    assert await unread_ids(store, "alice") == []
    assert await unread_ids(store, "observer") == [root["id"], later["id"]]


@pytest.mark.parametrize("invalid", [[], [0], [True], [-1], list(range(1, 102))])
async def test_ack_rejects_invalid_ids(store: Store, invalid: list[int]) -> None:
    with pytest.raises(ValueError, match="positive message IDs"):
        await store.mark_read("example.com", "alice", "general", invalid)


async def test_dm_thread_reply_does_not_deliver_to_third_party(store: Store) -> None:
    root = await store.send_dm("example.com", "alice", "bob", "DM root")
    self_reply = await store.send_dm("example.com", "alice", "bob", "@outsider", root["id"])
    assert await unread_ids(store, "bob") == [root["id"]]
    audience = await store.notification_recipients(
        "example.com", "alice", root["channel"], self_reply["id"]
    )
    assert audience == {"kind": "dm", "users": ["alice"], "viewers": ["alice", "bob"]}
    reply = await store.send_dm("example.com", "bob", "alice", "Answer @outsider", root["id"])
    assert await unread_ids(store, "alice") == [reply["id"]]
    assert await unread_ids(store, "outsider") == []
    broadcast = await store.send_dm("example.com", "alice", "bob", "Broadcast", root["id"], True)
    assert await unread_ids(store, "bob") == [root["id"], broadcast["id"]]
    with pytest.raises(ValueError):
        await store.mark_read("example.com", "outsider", root["channel"], reply["id"])


async def test_upgrade_preserves_legacy_unread_and_feed_once(tmp_path: Path) -> None:
    path = tmp_path / "legacy.sqlite3"
    legacy = SCHEMA.replace(
        "send_to_channel INTEGER NOT NULL DEFAULT 0 CHECK (send_to_channel IN (0, 1)), ", ""
    )
    async with aiosqlite.connect(path, isolation_level=None) as connection:
        await connection.executescript(legacy)
        await connection.execute(
            "INSERT INTO channels(org,name,description,created_by) "
            "VALUES ('example.com','general','','alice')"
        )
        await connection.executemany(
            "INSERT INTO messages(org,channel,user,text,parent_id) "
            "VALUES ('example.com','general','alice',?,?)",
            [("Root", None), ("Reply", 1), ("@visitor", None)],
        )
        await connection.execute(
            "INSERT INTO subscriptions(org,user,channel,last_read_id) "
            "VALUES ('example.com','bob','general',1)"
        )
        await connection.execute(
            "INSERT INTO mentions(org,user,channel,message_id) "
            "VALUES ('example.com','visitor','general',3)"
        )
    store = Store(path)
    await store.open()
    try:
        assert await unread_ids(store, "bob") == [2, 3]
        assert await unread_ids(store, "visitor") == [3]
        history = await store.history("example.com", "general")
        assert [message["id"] for message in history] == [1, 2, 3]
        assert history[1]["send_to_channel"] is True
        assert history[0]["reply_count"] == 1
        await store.mark_read("example.com", "visitor", "general", 3)
        await store.mark_read("example.com", "bob", "general", 3)
        await store.close()
        await store.open()
        assert await unread_ids(store, "visitor") == []
        assert await unread_ids(store, "bob") == [2]
        assert len(await store.history("example.com", "general")) == 3
    finally:
        await store.close()
