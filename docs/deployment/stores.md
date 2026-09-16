# Stores and hubs

Use this page when moving a server from the single-process quickstart to
multiple workers or machines. Each worker creates its own `Registry`, but all
workers serving the same MCP service must use the same logical session storage
and event log.

A load balancer can send initialization, a later tool call, and a question's
reply to different workers. The store makes records available on any worker;
the hub delivers replies and changes to the workers that are waiting for them.
See [How the server fits together](../pieces.md#why-store-and-hub-exist) for the
request flow.

## Choose backends for the deployment

| Deployment | Session store | Hub |
| --- | --- | --- |
| Tests or one process with disposable state | `MemorySessionStore` | `MemoryHub` |
| Several processes on one machine | `SqliteSessionStore` | `SqliteHub` |
| Several machines behind a load balancer | `RedisSessionStore` or `PostgresSessionStore` | `RedisHub` or `PostgresHub` |

The package includes four sets: memory for one process, SQLite for workers on
one machine, and Redis or PostgreSQL for workers on several. For anything else your
application supplies objects implementing the contracts below.

For one process, `Registry` creates both memory backends:

<!-- name: test_stores -->
```python
from aiohttp_tiny_mcp import Registry

registry = Registry("service", "1.0")
```

For multiple workers, pass `hub=` and `session_store=`. Subclass `Hub` and
`SessionStore` to write one: the methods are abstract, so a type checker
reports a wrong signature and Python refuses to build a class that is missing
one. The bundled backends are written that way.

<!-- name: async test_stores_subclass -->
```python
from aiohttp_tiny_mcp.hub import Hub
from aiohttp_tiny_mcp.sessions import SessionStore
from aiohttp_tiny_mcp.sqlite import SqliteHub, SqliteSessionStore

assert issubclass(SqliteHub, Hub)
assert issubclass(SqliteSessionStore, SessionStore)
```

They are protocols as well, so an object that has the methods and inherits
nothing is still accepted -- useful for a backend built on something that has
its own base class. Inheriting is the better default because nothing checks
the other kind until it runs.

Register the same handlers on every worker, and manage connections and cleanup
with your aiohttp application's lifecycle. The interfaces may use two tables in
one database; they do not require separate database services.

Every backend pair plugs into `Registry` in the same way: construct one
storage object, pass its store and hub adapters as `session_store=` and `hub=`,
then register `storage.cleanup_ctx` on the aiohttp app. The endpoint and
handlers are backend-agnostic. In a handler, use `ex.session` or `ex.sessions`
for application state; use a hub directly only for a custom transport or event
bridge.

Two `Registry` settings apply to every backend: `session_ttl_seconds` is the
TTL supplied whenever the server creates or refreshes a session, and
`hub_poll_seconds` is the longest one `Hub.poll` call waits before the caller
can observe cancellation or other work. They are distinct from event retention:
a session can remain usable after the event that announced it has expired.

## On SQLite

`pip install "aiohttp-tiny-mcp[sqlite]"` adds `aiosqlite` and with it
`aiohttp_tiny_mcp.sqlite`, which is the whole of what several workers on one
machine need:

<!-- name: async test_stores_sqlite -->
```python
from aiohttp import web

from aiohttp_tiny_mcp import Endpoint, Registry
from aiohttp_tiny_mcp.sqlite import SqliteHub, SqliteSessionStore, SqliteStorage

storage = SqliteStorage("mcp.sqlite")
registry = Registry(
    "service",
    "1.0",
    hub=SqliteHub(storage),
    session_store=SqliteSessionStore(storage),
)

app = web.Application()
# Opens the file for the application's lifetime and sweeps expired rows.
app.cleanup_ctx.append(storage.cleanup_ctx)
app.add_routes(Endpoint(registry).routes("/mcp"))
```

`SqliteStorage` owns one SQLite connection. `SqliteSessionStore` and
`SqliteHub` are the two adapters over that connection: pass both to the same
`Registry`, rather than opening connections or calling them for each request.
`cleanup_ctx` opens the connection when aiohttp starts, periodically removes
expired rows, and waits for that task before closing the connection on shutdown.

The endpoint then uses the backends itself:

- `SqliteSessionStore` keeps negotiated protocol sessions, application values
  written through `ex.session`, and state for an interrupted input round.
- `SqliteHub` carries replies to pushed questions, subscription changes, and
  HTTP+SSE replies to whichever worker owns the open stream.

In a handler, use `ex.session` or `ex.sessions` for application state; do not
normally instantiate `SqliteSessionStore` there. `Hub` is likewise transport
infrastructure. Use it directly only when implementing a custom transport or
an application event bridge.

### One storage per worker

One `SqliteStorage` is one connection to one file. Each worker constructs its
own `SqliteStorage`, `SqliteSessionStore`, and `SqliteHub`, all with the same
database path, and registers its own `cleanup_ctx`. Do not share a connection
object between event loops or processes.

```text
worker A: SqliteStorage("/var/lib/service/mcp.sqlite") ─┐
worker B: SqliteStorage("/var/lib/service/mcp.sqlite") ─┼─ same SQLite file
worker C: SqliteStorage("/var/lib/service/mcp.sqlite") ─┘
```

This coordinates the processes that can open that file: workers on one machine,
not machines. Separate files on separate servers are separate deployments,
whatever the paths are called. A network filesystem does not fix that -- SQLite
locking over NFS is not dependable.

### Tuning and maintenance

`SqliteHub(look_again=...)` controls how often a waiting reader checks for a
new event; SQLite cannot announce a new row. It does not extend a caller's
`poll` deadline. `SqliteStorage(event_ttl_seconds=...)` controls how long hub
events remain readable; a reader that falls farther behind misses them. Session
lifetimes are controlled separately by `Registry(session_ttl_seconds=...)`.

| Setting | Default | Use it to |
| --- | --- | --- |
| `SqliteStorage(path)` | required | Name the local database file; use persistent local storage in production. |
| `busy_timeout_ms` | `5000` | Wait for SQLite's single writer instead of immediately raising `database is locked`. |
| `event_ttl_seconds` | `3600` | Retain hub events for this long; it bounds how far a reader may lag. |
| `sweep_seconds` | `60` | Set the interval for the cleanup-context sweeper. |
| `SqliteHub(look_again=...)` | `0.25` | Trade idle polling queries for event-delivery latency. |

The defaults fit ordinary local multi-process deployments. Set the database
path to persistent local storage, and include the file in backup procedures if
session state matters during a restart. Expired session and event rows are
removed by the cleanup task. `await storage.sweep()` performs one pass now;
`await storage.sweeping(every=...)` is the same work on a loop outside aiohttp.

## On Redis

`pip install "aiohttp-tiny-mcp[redis]"` adds `redis` and with it
`aiohttp_tiny_mcp.redis`, which is what a deployment across machines needs:

<!-- name: async test_stores_redis -->
```python
from aiohttp import web

from aiohttp_tiny_mcp import Endpoint, Registry
from aiohttp_tiny_mcp.redis import RedisHub, RedisSessionStore, RedisStorage

pool = RedisStorage.from_url("redis://localhost")
registry = Registry(
    "service",
    "1.0",
    hub=RedisHub(pool),
    session_store=RedisSessionStore(pool),
)

app = web.Application()
app.cleanup_ctx.append(pool.cleanup_ctx)
app.add_routes(Endpoint(registry).routes("/mcp"))
```

`RedisStorage(prefix=...)` opens every key it owns, `mcp` by default, and the
store and the hub take that prefix unless told another. Pass the `Redis` an
application already has to the constructor -- `RedisStorage(client)` -- and it
stays the application's to close; `from_url` is for callers who keep none. As with PostgreSQL, every
setting is a class attribute too.

A session is one key. `create` uses `SET NX`, so two workers cannot both make
one, and an expired key is gone before the next `create` sees it. `save` is a
short Lua script that checks the version and writes in the same step, because
reading the version and updating after is not atomic.

A topic is a Redis stream. `publish` is `XADD`, which assigns ids in
publication order, so a stream id is a safe cursor. `poll` is `XREAD BLOCK`:
the reader waits in Redis and is woken when the event lands, rather than asking
again on a timer as the SQLite hub must. Reading does not consume, so every
worker watching a topic sees every event.

`RedisHub(keep=..., ttl_seconds=...)` bounds a topic: about that many events,
and that long after the last publish. `prefix=` sets the key prefix, which is
`mcp` by default, so one Redis can serve other things and a second deployment.

| Setting | Default | Use it to |
| --- | --- | --- |
| `RedisStorage.from_url(url, prefix=...)` | `prefix="mcp"` | Create a Redis client owned and closed by the storage. |
| `RedisStorage(client, prefix=...)` | `owned=False` | Reuse an application-owned client; the application closes it. |
| `RedisSessionStore(..., prefix=...)` | storage prefix | Put session keys in a separately named Redis namespace. |
| `RedisHub(..., prefix=...)` | storage prefix | Put topic streams in a separately named Redis namespace. |
| `RedisHub(keep=...)` | `1000` | Approximately cap retained events per topic; slow readers can miss trimmed events. |
| `RedisHub(ttl_seconds=...)` | `3600` | Expire a quiet topic this long after its last publish. |

## On PostgreSQL

`pip install "aiohttp-tiny-mcp[postgres]"` adds `psycopg` and with it
`aiohttp_tiny_mcp.postgres`, the other way to serve workers on several
machines:

<!-- name: async test_stores_postgres -->
```python
from aiohttp import web

from aiohttp_tiny_mcp import Endpoint, Registry
from aiohttp_tiny_mcp.postgres import PostgresHub, PostgresSessionStore, PostgresStorage

pool = PostgresStorage.from_url("postgresql://mcp@localhost/mcp")
registry = Registry(
    "service",
    "1.0",
    hub=PostgresHub(pool),
    session_store=PostgresSessionStore(pool),
)

app = web.Application()
app.cleanup_ctx.append(pool.cleanup_ctx)
app.add_routes(Endpoint(registry).routes("/mcp"))
```

Three tables are created on first use: the two above and `<prefix>_state`,
a keyed store this package remembers things in -- when the last sweep ran, for
one. `storage.remember(key, value)` and `storage.recall(key)` read and write
it, so a deployment has somewhere to keep its own small settings beside them. `PostgresStorage(prefix=...)`
names them -- `<prefix>_sessions` and `<prefix>_events`, `mcp` by default -- so
one database holds two deployments, or this package beside something else.
`create_tables=False` leaves the schema to your migrations, which then have to
produce what `aiohttp_tiny_mcp.postgres.SCHEMA` describes. Pass the pool an application already has
to the constructor -- `PostgresStorage(pool)` -- and it stays the
application's to close; `from_url` makes one for callers who keep none,
because a second pool is a second set of connections to one server.

Every setting is a class attribute as well as a constructor argument, so a
deployment states them once in a subclass rather than at each call:

<!-- name: async test_stores_postgres -->
```python
class OurStorage(PostgresStorage):
    prefix = "assistant"
    event_ttl_seconds = 600
    create_tables = False


assert OurStorage.from_url("postgresql://mcp@localhost/mcp").prefix == "assistant"
```

A waiting reader asks again on a timer, `PostgresHub(look_again=...)` apart.
`LISTEN` would wake it sooner, but a connection kept for notifications is one
this deployment cannot use for anything else, and a hundred workers waiting
that way spend a hundred of the server's `max_connections` doing nothing. The
cursor is what makes asking safe: a reader sees everything published after the
position it holds, whenever it looks. Expiry is decided by `now()` in the database, so workers whose clocks
disagree still agree on what has expired.

Publishing takes an advisory lock on the topic. Without it a sequence id would
not order a topic: two transactions can take ids in one order and commit in the
other, and a reader that moved past the higher id would never see the lower
one. The lock is per topic, so publishers to different topics do not wait for
each other.

`PostgresStorage(event_ttl_seconds=...)` is how long an event stays readable.

| Setting | Default | Use it to |
| --- | --- | --- |
| `PostgresStorage.from_url(conninfo, min_size=..., max_size=...)` | `4`, unbounded | Create a pool owned and closed by the storage. |
| `PostgresStorage(pool, ...)` | `owned=False` | Reuse an application-owned pool; the application closes it. |
| `prefix` | `"mcp"` | Name the `<prefix>_sessions`, `_events`, and `_state` tables. |
| `create_tables` | `True` | Let the package create the tables on first use, or use migrations matching `SCHEMA`. |
| `event_ttl_seconds` | `3600` | Retain hub events for this long. |
| `sweep_seconds` | `60` | Default interval for `sweeping()` and `sweep_if_due()`. |
| `PostgresHub(look_again=...)` | `0.25` | Trade polling queries for event-delivery latency. |

`storage.remember(key, value)` and `await storage.recall(key)` store a small
JSON-serializable deployment value in the `_state` table. They are for
coordination such as maintenance state, not a replacement for application data
or request sessions.

## Maintaining PostgreSQL

Redis uses Redis key and stream expiry, and SQLite's `cleanup_ctx` periodically
sweeps expired rows. PostgreSQL deliberately does neither from `cleanup_ctx`:
with two hundred workers, every one of them deleting the same rows every minute
is load rather than housekeeping. It is asked for instead, and answers who
sweeps by itself.

`storage.sweep_if_due()` is safe on every worker at once, which is what makes
"which of the two hundred?" stop being a question. Each pass:

1. reads when the last sweep was, which is one cheap query and usually the end
   of it;
2. takes `pg_try_advisory_xact_lock` where it looks due, and gives up where
   another worker holds it;
3. reads the time again under the lock, because two workers can both have
   found it due;
4. deletes, and records the time.

The lock ends with the transaction, so a worker that dies mid-sweep releases
it. `storage.sweeping()` is that on a loop, and every worker may run it:

<!-- name: async test_stores_postgres -->
```python
import asyncio

sweeping = asyncio.create_task(pool.sweeping())
# ...and cancelled with the rest of the application.
sweeping.cancel()
```

`storage.sweep()` deletes now without asking whether anybody else is, for a
cron job or a maintenance command that is meant to be the only one.

Nothing breaks while no one sweeps: an expired session already reads as absent
and an old event is already past every reader. What grows is the table.

## What goes where

| Data | Storage | Why it crosses requests or workers |
| --- | --- | --- |
| HTTP protocol session, client capabilities, log level, subscriptions | `SessionStore` | The next request can arrive on a different worker |
| Values explicitly written through `Session` | `SessionStore` | Later calls need the current values |
| Payload explicitly passed to `NeedInput(..., state=...)` | `SessionStore` | A follow-up call may restart the handler on another worker |
| Reply to a question waiting on an open stream | `Hub` | The reply request may arrive elsewhere |
| Resource and list change notifications | `Hub` | Subscribers may be connected to any worker |
| Active response stream, Python locals, resolved dependencies | The current worker | These belong to the current invocation and are not serialized |

Your business data still belongs in your application's storage. Synchronizing
MCP sessions does not synchronize a handler's module-level lists or dictionaries,
and does not make application operations transactional.

## The session store

Four methods. `create` and `save` report failure rather than overwriting, and
the backend supplies that atomicity -- a compare-and-set, a conditional update,
or a transaction.

<!-- name: test_stores -->
```python
from collections.abc import Mapping
from typing import Any, Protocol

from aiohttp_tiny_mcp.sessions import SessionRecord


class SessionStoreProtocol(Protocol):
    async def create(self, session_id: str, data: Mapping[str, Any], *, ttl_seconds: int) -> bool:
        """Create with a TTL; return False if a live record already exists."""

    async def get(self, session_id: str) -> SessionRecord | None:
        """The live record and its version, or None for missing/expired records."""

    async def save(
        self,
        session_id: str,
        data: Mapping[str, Any],
        *,
        expected_version: int,
        ttl_seconds: int,
    ) -> bool:
        """False if missing, expired, or the version differs. Otherwise
        replace data, advance the version, and renew the TTL atomically."""

    async def delete(self, session_id: str) -> None:
        """Forget it."""
```

Store JSON-serializable data only. Application values and explicit request
state must satisfy that requirement; do not pass an `Exchange`, socket, queue,
or task as state.

Treat expired records as absent in every operation. `create` must be able to
reuse an expired key, and `save` must not resurrect an expired record. Concurrent
creates for one key must have one winner. Concurrent saves with the same
`expected_version` must also have one winner: checking the version in Python
and updating later is not atomic.

A record carries its data and the version the store assigned:

<!-- name: test_stores -->
```python
record = SessionRecord(data={"n": 1}, version=3)
assert record.data["n"] == 1
```

The version is the store's own; this package reads it back rather than assuming
how it counts.

### On PostgreSQL

`aiohttp_tiny_mcp.postgres` implements this; see [On PostgreSQL](#on-postgresql-1)
below. One table is enough, and `version` does the compare-and-set while
`expires_at` does the expiry:

```sql
CREATE TABLE mcp_sessions (
    id          TEXT PRIMARY KEY,
    data        JSONB NOT NULL,
    version     BIGINT NOT NULL DEFAULT 1,
    expires_at  TIMESTAMPTZ NOT NULL
);
CREATE INDEX ON mcp_sessions (expires_at);
```

`get` selects only where `expires_at > now()`. `save` checks the ID, expected
version, and expiry in the same update that replaces data, advances the
version, and renews expiry. `create` atomically inserts an absent key or
replaces an expired record while refusing a live duplicate. An unconditional
`ON CONFLICT DO NOTHING` alone would leave expired keys unavailable until
cleanup.

## The hub

The hub is an ordered event log, not a work queue. Polling must not consume
messages: several workers may need to observe the same resource change. A
backend can implement it with database records and optional wake-up signals.

<!-- name: test_stores -->
```python
from collections.abc import Sequence

from aiohttp_tiny_mcp.hub import START, Cursor


class HubProtocol(Protocol):
    async def publish(self, topic: str, message: Mapping[str, Any]) -> None:
        """Append one message to a topic."""

    async def position(self, topic: str) -> Cursor:
        """Where the topic stands now."""

    async def poll(
        self, topic: str, cursor: Cursor, *, timeout: float
    ) -> tuple[Sequence[Mapping[str, Any]], Cursor]:
        """Messages after the cursor, and where to continue from."""

    async def delete(self, topic: str) -> None:
        """Forget a topic, once whatever it served is over."""


assert START == ""
```

A `Cursor` is an opaque string. `START` is the empty string. `position` returns
the current end of the topic; `poll` returns events strictly after its cursor,
in order, together with a position from which reading can continue. An empty
poll must preserve the reader's position. It must wait no longer than `timeout`
and should return promptly when events arrive.

Keep topic names and cursors stable across workers. The library already scopes
its topic and session keys with the deployment's
[namespace](multitenancy.md); do not strip that prefix.

### Cursors and delivery

The public contract is cursor-based: the reader holds a position and asks for
what came after it. A backend may use notifications internally to wake a waiting
poll, but a wake-up signal alone is not the event storage.

That is what lets two workers take part in one exchange without knowing about
each other. A question pushed to a client on worker A is answered by a request
that lands on worker B; B publishes the answer, A reads it, and neither has any
idea the other exists. The same shape carries subscriptions.

The waiting worker calls `position` before sending its question. It then calls
`poll` with that cursor. If the reply arrives before the first poll, the event
is already in storage after the captured cursor and the poll returns it.

### How `poll` waits is yours

This package states a deadline and never a polling interval.

A backend that can be told when a row lands should answer at once --
`LISTEN`/`NOTIFY` on PostgreSQL, a condition variable in memory. One that
cannot may sleep and look again. Neither is visible from here.

<!-- name: async test_hub_poll -->
```python
import asyncio

from aiohttp_tiny_mcp import MemoryHub

hub = MemoryHub()
cursor = await hub.position("demo")

# Nothing yet, and `poll` returns empty at the deadline rather than blocking.
messages, cursor = await hub.poll("demo", cursor, timeout=0.01)
assert messages == []

await hub.publish("demo", {"jsonrpc": "2.0", "method": "notifications/tools/list_changed"})
messages, cursor = await hub.poll("demo", cursor, timeout=1)
assert [message["method"] for message in messages] == ["notifications/tools/list_changed"]
```

`Registry(hub_poll_seconds=...)` sets how long one `poll` may wait. It is a
deadline, not an interval.

### On PostgreSQL

```sql
CREATE TABLE mcp_events (
    id       BIGSERIAL PRIMARY KEY,
    topic    TEXT NOT NULL,
    message  JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ON mcp_events (topic, id);
```

The read shape is `WHERE topic = $1 AND id > $2 ORDER BY id`, with the last
returned ID as the next cursor. An empty topic uses `START`. However, a sequence
ID alone does not guarantee publication order: concurrent transactions can
allocate IDs in one order and commit in another. If a reader advances past an
uncommitted lower ID, it will miss that event. A backend must serialize
publication per topic or otherwise ensure that advancing a cursor cannot skip
an event that becomes visible later.

`LISTEN`/`NOTIFY` can wake waiting readers, or `poll` can check storage repeatedly
until its deadline. In either case, check the event records so a publish between
capturing the cursor and starting the wait remains visible.

Completed question topics are deleted by the library. For notification topics,
choose a retention and cleanup policy that accounts for lagging readers. The
`poll` timeout is how long one wait lasts, not how long a reader may fall behind.
`MemoryHub` retains notification events in process memory; it is not a bounded
persistent event log.

## Running more than one worker

Before sending traffic to several workers, verify that:

1. A session created on A can be read and updated on B, and concurrent writes
   with the same version cannot both succeed.
2. An event published on B can be read by A using a cursor captured before the
   publish, even if polling starts after the publish.
3. Both workers use the same namespace for the same tenant and load the same
   handler declarations and compatible protocol adapters.
4. Business data and operations have their own cross-worker storage and
   concurrency rules; they do not depend on one worker's Python globals.

With both shared backends, protocol requests do not need sticky routing to the
worker that initialized the session. A running call and its open stream still
belong to one worker. If that worker exits, the backends do not move its
coroutine or connection elsewhere, and they do not provide exactly-once
execution of your business operation.

A pushed question holds the response stream while the user answers. Configure
proxy buffering and idle timeouts to allow this; see
[Streamable HTTP](transports.md#streamable-http). Application retry behavior
must account for whether an operation already changed business data.

## Timeouts

| Setting | Default | What it bounds |
| --- | --- | --- |
| `session_ttl_seconds` | 3600 | How long a session lives without use |
| `request_state_ttl_seconds` | 600 | How long a round trip may take |
| `hub_poll_seconds` | 30.0 | How long one `poll` may wait |
| `ask_timeout_seconds` | 120.0 | How long a pushed question waits for an answer |

When an open-stream question reaches `ask_timeout_seconds`, `ex.ask` returns
an answer with action `cancel`. Expired request state is different: a later call
with that state identifier is rejected. See [Sessions and state](../guide/sessions.md)
for handler-facing storage APIs.
