import sqlite3

from aiohttp_tiny_mcp.storage.sqlite import SCHEMA


def test_expired_events_use_time_index_without_scanning_live_events() -> None:
    with sqlite3.connect(":memory:") as connection:
        connection.executescript(SCHEMA)
        connection.executemany(
            "INSERT INTO mcp_events(topic,message,created_at) VALUES (?,?,?)",
            [("topic", "{}", number) for number in range(10000)],
        )
        sql = "DELETE FROM mcp_events WHERE created_at <= ?"
        details = "\n".join(row[3] for row in connection.execute("EXPLAIN QUERY PLAN " + sql, (9,)))
        assert "mcp_events_expiry" in details
        assert "SCAN mcp_events" not in details
        connection.execute(sql, (9,))
        assert connection.execute("SELECT count(*) FROM mcp_events").fetchone()[0] == 9990
