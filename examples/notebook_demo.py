"""SQLite-backed notebook MCP demo with tools, resources, prompts, completion, and notifications.

    uv run python examples/notebook_demo.py
    uv run python examples/notebook_demo.py --stdio

Serves /mcp on port 8090. See examples/demo.py for sample HTTP requests.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiosqlite
from aiohttp import web
from pydantic import BaseModel, Field

from aiohttp_tiny_mcp import (
    Endpoint,
    Exchange,
    MemoryHub,
    MemorySessionStore,
    NeedInput,
    Registry,
    elicit,
    run_stdio,
)
from aiohttp_tiny_mcp.hub import NOTIFICATIONS, topic
from aiohttp_tiny_mcp.models import CompleteParams


@dataclass
class Entry:
    id: int
    kind: str
    title: str
    body: str
    tags: list[str]
    done: bool
    created_at: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "title": self.title,
            "body": self.body,
            "tags": self.tags,
            "done": self.done,
            "createdAt": self.created_at,
        }


class NoteStore:
    """The notebook itself, on one SQLite file.

    This is the application's own data, not the protocol's: sessions and
    events go to `aiohttp_tiny_mcp.sqlite`, which keeps its own tables.
    """

    def __init__(self, path: str) -> None:
        self.path = path
        self.connection: aiosqlite.Connection | None = None
        self.opening = asyncio.Lock()

    async def open(self) -> aiosqlite.Connection:
        """Connect lazily, so this can be built before there is an event loop."""
        async with self.opening:
            if self.connection is None:
                connection = await aiosqlite.connect(self.path)
                connection.row_factory = aiosqlite.Row
                await connection.execute("PRAGMA journal_mode=WAL")
                await connection.execute(
                    """CREATE TABLE IF NOT EXISTS entries (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        kind TEXT NOT NULL,
                        title TEXT NOT NULL,
                        body TEXT NOT NULL DEFAULT '',
                        tags TEXT NOT NULL DEFAULT '',
                        done INTEGER NOT NULL DEFAULT 0,
                        created_at REAL NOT NULL
                    )"""
                )
                await connection.commit()
                self.connection = connection
            return self.connection

    async def close(self) -> None:
        if self.connection is not None:
            await self.connection.close()
            self.connection = None

    def row_to_entry(self, row: aiosqlite.Row) -> Entry:
        tags = [t for t in row["tags"].split(",") if t]
        return Entry(
            id=row["id"],
            kind=row["kind"],
            title=row["title"],
            body=row["body"],
            tags=tags,
            done=bool(row["done"]),
            created_at=row["created_at"],
        )

    async def rows(self, sql: str, parameters: tuple = ()) -> list[Entry]:
        connection = await self.open()
        async with connection.execute(sql, parameters) as cursor:
            return [self.row_to_entry(row) for row in await cursor.fetchall()]

    async def add(self, kind: str, title: str, body: str, tags: list[str]) -> Entry:
        connection = await self.open()
        cursor = await connection.execute(
            "INSERT INTO entries (kind, title, body, tags, done, created_at) "
            "VALUES (?, ?, ?, ?, 0, ?)",
            (kind, title, body, ",".join(tags), time.time()),
        )
        await connection.commit()
        found = await self.get(cursor.lastrowid or 0)
        assert found is not None, "the row just written is not there"
        return found

    async def get(self, entry_id: int) -> Entry | None:
        found = await self.rows("SELECT * FROM entries WHERE id = ?", (entry_id,))
        return found[0] if found else None

    async def all(self) -> list[Entry]:
        return await self.rows("SELECT * FROM entries ORDER BY id DESC")

    async def open_todos(self) -> list[Entry]:
        return await self.rows("SELECT * FROM entries WHERE kind = 'todo' AND done = 0 ORDER BY id")

    async def complete_todo(self, entry_id: int) -> Entry | None:
        connection = await self.open()
        await connection.execute(
            "UPDATE entries SET done = 1 WHERE id = ? AND kind = 'todo'", (entry_id,)
        )
        await connection.commit()
        return await self.get(entry_id)

    async def delete(self, entry_id: int) -> None:
        connection = await self.open()
        await connection.execute("DELETE FROM entries WHERE id = ?", (entry_id,))
        await connection.commit()

    async def search(self, query: str, tag: str | None = None) -> list[Entry]:
        sql = "SELECT * FROM entries WHERE (title LIKE ? OR body LIKE ?)"
        parameters: list[Any] = [f"%{query}%", f"%{query}%"]
        if tag:
            sql += " AND (',' || tags || ',') LIKE ?"
            parameters.append(f"%,{tag},%")
        return await self.rows(sql + " ORDER BY id DESC", tuple(parameters))

    async def tags(self) -> list[str]:
        seen: dict[str, None] = {}
        for entry in await self.all():
            for tag in entry.tags:
                seen[tag] = None
        return list(seen)

    async def by_tag(self, tag: str) -> list[Entry]:
        return await self.rows(
            "SELECT * FROM entries WHERE (',' || tags || ',') LIKE ? ORDER BY id",
            (f"%,{tag},%",),
        )


STORE = web.AppKey("notebook_store", NoteStore)


hub = MemoryHub()  # Replace with a shared event source when running multiple workers.
# Replace process-local backing services with shared stores for multiple workers.
registry = Registry(
    "notebook",
    "0.1.0",
    hub=hub,
    instructions="A project notebook: notes and TODOs.",
    session_store=MemorySessionStore(),
)
registry.provide(NoteStore, STORE)


async def notify_change(entry_id: int) -> None:
    for uri in ("notebook://entries", "notebook://todos/open", f"notebook://entries/{entry_id}"):
        await hub.publish(
            topic(NOTIFICATIONS),
            {"jsonrpc": "2.0", "method": "notifications/resources/updated", "params": {"uri": uri}},
        )


class Nothing(BaseModel):
    pass


class AddNote(BaseModel):
    title: str
    body: str = ""
    tags: list[str] = Field(default_factory=list)


@registry.tool
async def add_note(args: AddNote, store: NoteStore) -> str:
    """Add a note to the notebook."""
    entry = await store.add("note", args.title, args.body, args.tags)
    await notify_change(entry.id)
    return f"added note #{entry.id}: {entry.title}"


class AddTodo(BaseModel):
    title: str
    body: str = ""
    tags: list[str] = Field(default_factory=list)


@registry.tool
async def add_todo(args: AddTodo, store: NoteStore) -> str:
    """Add an open TODO to the notebook."""
    entry = await store.add("todo", args.title, args.body, args.tags)
    await notify_change(entry.id)
    return f"added todo #{entry.id}: {entry.title}"


class EntryId(BaseModel):
    id: int


@registry.tool
async def complete_todo(args: EntryId, store: NoteStore) -> str:
    """Mark a TODO as done."""
    entry = await store.complete_todo(args.id)
    if entry is None:
        return f"no open todo #{args.id}"
    await notify_change(entry.id)
    return f"completed #{entry.id}: {entry.title}"


@registry.tool
async def delete_entry(args: EntryId, ex: Exchange, store: NoteStore) -> str:
    """Delete a note or TODO after user confirmation."""
    if (answer := ex.answers.get("confirm")) is not None:
        if not answer.get("ok"):
            return "cancelled"
        await store.delete(args.id)
        await notify_change(args.id)
        return f"deleted #{args.id}"
    entry = await store.get(args.id)
    if entry is None:
        return f"no entry #{args.id}"
    if not ex.can_ask:
        return f"this client can't confirm destructive actions -- refusing to delete #{args.id}"
    raise NeedInput(
        {
            "confirm": elicit(
                f"Delete {entry.kind} #{entry.id} ({entry.title!r})? This can't be undone.",
                {"type": "object", "properties": {"ok": {"type": "boolean"}}},
            )
        }
    )


class Search(BaseModel):
    query: str
    tag: str | None = None


@registry.tool
async def search(args: Search, store: NoteStore) -> list[dict[str, Any]]:
    """Search notes and TODOs by title/body, optionally filtered by tag."""
    return [e.as_dict() for e in await store.search(args.query, args.tag)]


class ImportNotes(BaseModel):
    text: str = Field(description="One entry per line: 'note: Title: body' or 'todo: Title: body'.")


@registry.tool(streaming=True)
async def import_notes(args: ImportNotes, ex: Exchange, store: NoteStore) -> str:
    """Import notes/TODOs from lines, reporting progress as each is added."""
    lines = [line for line in args.text.splitlines() if line.strip()]
    added = 0
    for i, line in enumerate(lines):
        await ex.progress(i, len(lines), message=line[:60])
        kind, _, rest = line.partition(":")
        kind = kind.strip().lower()
        if kind not in ("note", "todo"):
            continue
        title, _, body = rest.strip().partition(":")
        entry = await store.add(kind, title.strip(), body.strip(), [])
        await notify_change(entry.id)
        added += 1
        await asyncio.sleep(0.05)  # slow enough to actually watch progress arrive
    return f"imported {added} entries"


@registry.resource("notebook://entries", mime_type="application/json")
async def entries(args: Nothing, store: NoteStore) -> list[dict[str, Any]]:
    """Every note and TODO, most recent first."""
    return [e.as_dict() for e in await store.all()]


class EntryRef(BaseModel):
    id: str


@registry.resource("notebook://entries/{id}", name="entry", mime_type="application/json")
async def entry(args: EntryRef, store: NoteStore) -> dict[str, Any]:
    """One entry by id."""
    try:
        entry_id = int(args.id)
    except ValueError:
        return {"error": f"invalid id {args.id!r}"}
    found = await store.get(entry_id)
    if found is None:
        return {"error": f"no entry #{entry_id}"}
    return found.as_dict()


@registry.resource("notebook://todos/open", mime_type="application/json")
async def open_todos(args: Nothing, store: NoteStore) -> list[dict[str, Any]]:
    """Just the open TODOs."""
    return [e.as_dict() for e in await store.open_todos()]


class StandupSummary(BaseModel):
    tag: str | None = None


@registry.prompt
async def standup_summary(args: StandupSummary, store: NoteStore) -> str:
    """Draft a standup update from open TODOs and recent notes."""
    todos = await store.open_todos()
    recent = (await store.all())[:10]
    if args.tag:
        todos = [e for e in todos if args.tag in e.tags]
        recent = [e for e in recent if args.tag in e.tags]
    todo_ids = {e.id for e in todos}
    combined = todos + [e for e in recent if e.id not in todo_ids]
    lines = "\n".join(f"- [{e.kind}{'/done' if e.done else ''}] {e.title}" for e in combined)
    return (
        f"Here are the current notebook entries{f' tagged {args.tag!r}' if args.tag else ''}:\n\n"
        f"{lines}\n\nWrite a concise standup update (done / next / blockers) from this."
    )


class ReleaseNotes(BaseModel):
    tag: str


@registry.prompt
async def release_notes(args: ReleaseNotes, store: NoteStore) -> str:
    """Draft changelog-style release notes from entries tagged `tag`."""
    tagged = await store.by_tag(args.tag)
    done = [e for e in tagged if e.kind == "note" or e.done]
    lines = "\n".join(f"- {e.title}: {e.body}" for e in done)
    return f"Draft release notes from these items tagged {args.tag!r}:\n\n{lines}"


@registry.completions
async def complete(args: CompleteParams, store: NoteStore) -> list[str]:
    """Complete prompt tags and notebook://entries/{id} resource ids."""
    if args.ref.type == "ref/prompt" and args.argument.name == "tag":
        tags = await store.tags()
        return [t for t in tags if t.startswith(args.argument.value)]
    if args.ref.type == "ref/resource" and args.argument.name == "id":
        ids = [str(e.id) for e in await store.all()]
        return [i for i in ids if i.startswith(args.argument.value)]
    return []


async def health(request: web.Request) -> web.Response:
    return web.Response(text="ok")


async def closing_the_store(app: web.Application):
    """Hold the notebook open for the application's lifetime."""
    yield
    await app[STORE].close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stdio", action="store_true", help="serve over stdio instead of HTTP")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    db_path = str(Path(__file__).parent / "notebook_demo.sqlite3")
    if args.stdio:
        # stdout is reserved for the stdio protocol.
        logging.basicConfig(level=logging.INFO, stream=sys.stderr)
        store = NoteStore(db_path)
        try:
            asyncio.run(run_stdio(registry, app_state={STORE: store}))
        finally:
            asyncio.run(store.close())
    else:
        logging.basicConfig(level=logging.INFO)
        logging.getLogger("aiohttp_tiny_mcp").setLevel(logging.DEBUG)
        app = web.Application()
        app[STORE] = NoteStore(db_path)
        app.cleanup_ctx.append(closing_the_store)
        app.add_routes([web.get("/health", health)])
        Endpoint(
            registry,
            # Behind a reverse proxy, uncomment this and let the proxy check Origin.
            # trust_proxy_origin_validation=True,
        ).setup(app, "/mcp")
        print(f"notebook data: {db_path}")
        web.run_app(app, host="127.0.0.1", port=8090)
