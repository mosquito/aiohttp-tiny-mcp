# API

## Declaring what a server offers

```{eval-rst}
.. autoclass:: aiohttp_tiny_mcp.Registry
   :members:
```

## Extensions and skills

```{eval-rst}
.. autoclass:: aiohttp_tiny_mcp.Extension
   :members: method, resource

.. autoclass:: aiohttp_tiny_mcp.skills.Skills
   :members: from_directory
```

See [Extensions and skills](../guide/extensions.md) for registration and legacy
resource URIs. Loading directories requires `aiohttp-tiny-mcp[skills]`.

## Serving it

```{eval-rst}
.. autoclass:: aiohttp_tiny_mcp.Endpoint
   :members: app, setup, routes, view

.. autoclass:: aiohttp_tiny_mcp.SseEndpoint
   :members: setup, routes

.. autofunction:: aiohttp_tiny_mcp.run_stdio
```

## Inside a handler

```{eval-rst}
.. autoclass:: aiohttp_tiny_mcp.Exchange
   :members: ask, answered, accepted, action, answers, log, logs, progress,
             session, sessions, state, client_info, can_ask, cancel,
             wait_cancelled

.. autofunction:: aiohttp_tiny_mcp.elicit

.. autofunction:: aiohttp_tiny_mcp.elicit_accept

.. autofunction:: aiohttp_tiny_mcp.elicit_decline

.. autofunction:: aiohttp_tiny_mcp.elicit_cancel

.. autoclass:: aiohttp_tiny_mcp.NeedInput

.. autoclass:: aiohttp_tiny_mcp.core.Answer
   :members:
```

## Sessions and state

```{eval-rst}
.. autoclass:: aiohttp_tiny_mcp.sessions.Session
   :members:

.. autoclass:: aiohttp_tiny_mcp.sessions.SessionAccess
   :members:

.. autoclass:: aiohttp_tiny_mcp.sessions.SessionStore
   :members:

.. autoclass:: aiohttp_tiny_mcp.MemorySessionStore

.. autoclass:: aiohttp_tiny_mcp.request_state.RequestStates
   :members:
```

## Events

```{eval-rst}
.. autoclass:: aiohttp_tiny_mcp.hub.Hub
   :members:

.. autoclass:: aiohttp_tiny_mcp.hub.Event

.. autoclass:: aiohttp_tiny_mcp.hub.Subscription
   :members:

.. autoclass:: aiohttp_tiny_mcp.MemoryHub

.. autofunction:: aiohttp_tiny_mcp.hub.topic
```

## On SQLite

Both backends on one file, for workers on one machine. Needs `aiosqlite`:
install `aiohttp-tiny-mcp[sqlite]`. See [Stores and hubs](../deployment/stores.md#on-sqlite).

```{eval-rst}
.. autoclass:: aiohttp_tiny_mcp.sqlite.SqliteStorage
   :members:

.. autoclass:: aiohttp_tiny_mcp.sqlite.SqliteSessionStore

.. autoclass:: aiohttp_tiny_mcp.sqlite.SqliteHub
```

## Streams

```{eval-rst}
.. autoclass:: aiohttp_tiny_mcp.SSEResponse
   :members: send, send_json, comment, close, closed

.. autoclass:: aiohttp_tiny_mcp.sse.SSEEvent
   :members: to_bytes

.. autofunction:: aiohttp_tiny_mcp.sse.read_sse
```

## On Redis

Both backends on a Redis server, for workers on more than one machine. Needs
`redis`: install `aiohttp-tiny-mcp[redis]`. See
[Stores and hubs](../deployment/stores.md#on-redis).

```{eval-rst}
.. autoclass:: aiohttp_tiny_mcp.redis.RedisStorage
   :members:

.. autoclass:: aiohttp_tiny_mcp.redis.RedisSessionStore

.. autoclass:: aiohttp_tiny_mcp.redis.RedisHub
```

## On PostgreSQL

Both backends on a PostgreSQL server. Needs `psycopg`: install
`aiohttp-tiny-mcp[postgres]`. See
[Stores and hubs](../deployment/stores.md#on-postgresql-1).

```{eval-rst}
.. autoclass:: aiohttp_tiny_mcp.postgres.PostgresStorage
   :members:

.. autoclass:: aiohttp_tiny_mcp.postgres.PostgresSessionStore

.. autoclass:: aiohttp_tiny_mcp.postgres.PostgresHub
```

## Many tenants

```{eval-rst}
.. autofunction:: aiohttp_tiny_mcp.namespaces.scoped
```

## Authentication

```{eval-rst}
.. autoclass:: aiohttp_tiny_mcp.auth.Authorization
   :members:

.. autoclass:: aiohttp_tiny_mcp.auth.Principal
   :members: holds, expired, identity

.. autoclass:: aiohttp_tiny_mcp.auth.TokenVerifier
```

## Clients

```{eval-rst}
.. autoclass:: aiohttp_tiny_mcp.Client
   :members: initialize, list_tools, call_tool, list_resources, read_resource,
             list_prompts, get_prompt, complete, listen, set_log_level, request_method

.. autoclass:: aiohttp_tiny_mcp.stdio_client.StdioClient
   :members: spawn

.. autoclass:: aiohttp_tiny_mcp.ClientError
```

## Revisions

```{eval-rst}
.. autoclass:: aiohttp_tiny_mcp.adapter.Adapter
   :members: version, can_ask, can_push_ask, asks_in_arguments, has_handshake,
             allows_batch, carries_state, decode, encode, describe_tool,
             capabilities

.. autoclass:: aiohttp_tiny_mcp.protocol.selection.AdapterSet
   :members:
```

## The normalized core

```{eval-rst}
.. autoclass:: aiohttp_tiny_mcp.core.Operation
   :members:
   :undoc-members:

.. autoclass:: aiohttp_tiny_mcp.core.FailureKind
   :members:
   :undoc-members:

.. autoclass:: aiohttp_tiny_mcp.core.Call

.. autoclass:: aiohttp_tiny_mcp.core.Value

.. autoclass:: aiohttp_tiny_mcp.core.NeedsInput

.. autoclass:: aiohttp_tiny_mcp.core.Failure

.. autoclass:: aiohttp_tiny_mcp.core.ClientProfile
```
