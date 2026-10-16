# Client OAuth

`OAuth` obtains MCP access tokens through discovery, Authorization Code with
PKCE, and refresh. It works with compatible authorization servers without
GitHub, Google, or other provider adapters. Server-side upstream integration
remains the responsibility of `OAuthProvider`.

An `OAuth` object belongs to one MCP endpoint and one application account. It
can serve several successive `Client` sessions. A request can refresh an
existing token, but it never opens a browser. If a login is necessary, it raises
`AuthorizationRequired`.

## Local browser login

Use `LoopbackCallback` for a CLI or desktop application. It starts an aiohttp
server on `127.0.0.1`. A fixed port is useful for a pre-registered callback;
the default port is allocated by the operating system.

Register the exact redirect URI with the authorization server before using a
static client ID. The following example uses `http://127.0.0.1:8765/callback`.

<!-- name: test_oauth_cli_definition -->
```python
from aiohttp_tiny_mcp import AdapterSet, Client, LoopbackCallback, OAuth


async def list_private_tools(endpoint: str, issuer: str, client_id: str):
    adapter = AdapterSet.default().by_version["2026-07-28"]
    async with LoopbackCallback(port=8765) as callback:
        async with OAuth(
            endpoint,
            issuer=issuer,
            client_id=client_id,
            redirect_uri=callback.redirect_uri,
        ) as auth:
            await callback.login(auth)
            async with Client(endpoint, adapter, auth=auth) as client:
                await client.initialize()
                return await client.list_tools()
```

A runnable version is available as `examples/client_oauth.py`:

```console
uv run python examples/client_oauth.py https://mcp.example.com/mcp \
  --issuer https://auth.example.com/tenant --client-id example-client \
  --port 8765 --success-html success.html
```

Omit `--port` to let the operating system select a free port. The helper binds
to port `0` and includes the assigned port in `callback.redirect_uri`.

`callback.login(auth)` opens the browser, receives the authorization response,
exchanges the code, and saves the credentials before reporting success. Pass
an async `open_browser(url)` callback to control how the browser is opened.
It must return after opening the URL, or after the browser request completes.

The OAuth object owns a separate HTTP session for discovery, registration, and
token requests. MCP credentials and cookies are not sent on those requests.
`Client(session=...)` retains its existing session ownership rules. Do not
combine `auth=OAuth(...)` with `Authorization` headers or default authentication
on that MCP session.

## Custom success HTML

Pass trusted application HTML through `success_html`. Both callback helpers
serve it unchanged after the token exchange and the `save()` callback succeed.
No callback parameters or tokens are inserted into the HTML. Failure returns a
generic error page. The application receives the original exception through
`login()` or `wait()`.

<!-- name: test_oauth_custom_html -->
```python
from aiohttp_tiny_mcp import LoopbackCallback

callback = LoopbackCallback(
    success_html="""<!doctype html>
<html lang="en"><meta charset="utf-8"><title>Connected</title>
<h1>Connected to Example</h1><p>Return to the application.</p></html>""",
)
```

Result pages disable caching and referrers. Their Content Security Policy
allows inline styles and HTTPS/data images; it blocks scripts and embedding
in a frame.

## Remote callback in an aiohttp application

The redirect URI can be an external HTTPS URL. `OAuthCallback` mounts on an
existing `aiohttp.web.Application`; it does not open a port or a browser.
`LoopbackCallback` is the local convenience wrapper around this same handler.

The following factory serves one account context. Application authentication
middleware must set `request["account_id"]` before the start route can run.
For multiple users, resolve a separate OAuth/callback context for each user;
do not share one object's credentials between accounts.

<!-- name: test_oauth_remote_definition -->
```python
from aiohttp import web

from aiohttp_tiny_mcp import OAuth, OAuthCallback


def make_app(auth: OAuth, account_id: str) -> web.Application:
    callback = OAuthCallback(
        auth,
        success_html="<!doctype html><h1>Connected</h1><p>Return to Example.</p>",
    )
    app = web.Application()

    async def oauth_lifetime(app):
        async with auth:
            yield
        callback.cancel()

    async def start(request):
        if request.get("account_id") != account_id:
            raise web.HTTPUnauthorized()
        attempt = await callback.begin()
        raise web.HTTPFound(location=attempt.url)

    app.cleanup_ctx.append(oauth_lifetime)
    app.router.add_post("/connect", start)
    callback.setup(app)
    return app


auth = OAuth(
    "https://mcp.example.com/mcp",
    issuer="https://auth.example.com/tenant",
    client_id="example-web-client",
    redirect_uri="https://app.example.com/oauth/callback",
)
app = make_app(auth, account_id="account-123")
```

`callback.setup(app)` adds `GET /oauth/callback`, using the path in
`auth.redirect_uri`. `callback.routes()` returns the route for manual mounting.
The handler uses the configured public URI, so it also works behind a reverse
proxy. It does not derive the public URI from `Host` or `Forwarded` headers.
Protect the start route with the application's normal session and CSRF checks.
Disable or redact callback query strings in the application's access logs.

Pending login attempts stay in memory. Route the callback to the process that
started the attempt. Starting a new attempt replaces the previous one for that
context. Use `callback.wait()` when an application task needs the result of
the token exchange; it is optional for ordinary HTTP redirect flows.

## Application-managed interaction

For another UI or callback mechanism, supply an async function to
`auth.login(authorize=...)`. The function receives the authorization URL and
returns the full callback URL. OAuth validates the URI, state, issuer, and
response before sending the code to the token endpoint.

<!-- name: test_oauth_application_ui -->
```python
from aiohttp_tiny_mcp import OAuth
from aiohttp_tiny_mcp.client.oauth import Authorize


async def sign_in(auth: OAuth, open_and_wait: Authorize) -> None:
    await auth.login(authorize=open_and_wait)
```

`await auth.begin()` and `await auth.complete(attempt, callback_url)` expose the
same flow in two steps. Use `auth.cancel(attempt)` to discard an attempt.
Attempts are single-use and expire after `login_timeout` seconds, default 300.

## Registration and discovery

The client follows Protected Resource Metadata from `WWW-Authenticate`, with
path and root well-known fallbacks. It supports both OAuth authorization-server
metadata and OIDC discovery. OIDC metadata support does not require OIDC login
or ID-token validation.

| Configuration | Behavior |
| --- | --- |
| `client_id` and `issuer` | Use a client registered with that exact issuer. |
| `metadata_url="https://app.example.com/client.json"` | Use a Client ID Metadata Document when the server supports it. The application hosts this document. |
| `registration={...}` | Allow Dynamic Client Registration when the server exposes a registration endpoint. `{}` uses the helper's defaults. |

The selection order is static registration, configured CIMD, then explicitly
allowed DCR. DCR is retained for compatibility. Its default metadata requests
Authorization Code and refresh grants, uses public-client authentication, and
chooses `native` for an HTTP loopback callback or `web` for HTTPS. Set
`registration={"application_type": "native"}` for an HTTPS native-app callback.
Registration failures raise an exception rather than changing the callback URI.

Static credentials and stored DCR credentials are bound to their issuer. If
metadata advertises a different issuer, the client stops instead of forwarding
those credentials. If several authorization servers are listed, select one
with `issuer=`. Registration state is saved with tokens when storage callbacks
are configured.

For a confidential static client, supply `client_secret` and
`token_endpoint_auth_method="client_secret_basic"` or `"client_secret_post"`.
Public clients use `"none"`. Private-key JWT authentication is not implemented.

The client requires advertised PKCE S256 support and sends the MCP `resource`
on authorization and token requests. An advertised response issuer must be
present and match exactly. An unadvertised `iss` is checked if present.
Scopes from a server challenge are combined with explicitly requested scopes;
without either, the client uses the resource metadata's scope list.

These rules follow the
[MCP authorization profile](https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization),
including its [registration order](https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization/client-registration).
The flow operates independently of the JSON-RPC adapter. An older wire revision
can use it when its server supplies this authorization profile. Historical
OAuth discovery profiles and STDIO authorization are not implemented here.

## Storage callbacks

By default, credentials stay in the OAuth object's memory. To retain them
between process runs, supply both callbacks:

<!-- name: test_oauth_storage_types -->
```python
from collections.abc import Awaitable, Callable
from typing import Any

Credentials = dict[str, Any]
LoadCredentials = Callable[[], Awaitable[Credentials | None]]
SaveCredentials = Callable[[Credentials], Awaitable[None]]
```

`load()` returns the full stored record, or `None` if no record exists.
`save(record)` replaces the full record and returns after storage succeeds.
Exceptions propagate; a read failure is not treated as an empty store.
The library defines and validates the record format: version, issuer, resource,
client ID, registration source and data, tokens, requested scopes, and expiry.
Preserve the complete record instead of reconstructing it from the access token.

Bind callbacks to an application account and connection. For example, this
in-memory adapter demonstrates the callback boundary; replace its operations
with the application's storage calls:

<!-- name: async test_oauth_storage_callbacks -->
```python
from copy import deepcopy


def credentials_callbacks(records, account_id, connection_id):
    key = (account_id, connection_id)

    async def load():
        return deepcopy(records.get(key))

    async def save(credentials):
        records[key] = deepcopy(credentials)

    return load, save


records = {}
load, save = credentials_callbacks(records, "alice", "reports")
assert await load() is None
await save({"version": 1})
assert await load() == {"version": 1}
```

Pass these callbacks to `OAuth(..., load=load, save=save)`. A later OAuth object
with the same configuration restores credentials on first use. It can refresh
and make MCP requests without calling `login()` again. Storage contains secrets;
the application controls access and encryption.

The library serializes token refresh within one OAuth instance. If multiple
instances or processes share credentials, the application must coordinate the
complete load, refresh, and save cycle. Atomic database writes alone do not
prevent concurrent refresh requests. For example, two processes can read the
same refresh token from SQLite before either saves a replacement.

Use a single refresh owner, or coordinate access outside the library and read
current credentials after obtaining exclusive access. An existing OAuth object
caches credentials; locking `save()` or reusing a stale object does not reload
them. The simple callbacks do not provide distributed transactions or locks.

## Refresh and errors

Refresh is automatic when a token expires or a request receives HTTP 401.
Concurrent callers of the same OAuth object wait for one refresh operation.
An MCP request gets at most one retry with a refreshed token. HTTP timeouts and
ambiguous network failures do not cause automatic replay of MCP operations.

Before a refresh request, the client saves a record without usable tokens.
This prevents automatic reuse of a refresh token if the server rotates it but
the response is lost. After success, the client saves the replacement before
using it. If the server omits a new refresh token, the old one is retained.
A storage or network failure between these steps can require another login.
There is no atomic transaction spanning application storage and the external
OAuth server.

`AuthorizationRequired.scopes` identifies the scopes needed for explicit login.
For `403 insufficient_scope`, it includes previously requested scopes and the
new challenge. A token without a refresh token requires login when it expires.
The client does not infer expiry by decoding access tokens as JWTs.

`RegistrationRequired` means the application must provide or renew client
registration. Other protocol failures raise `OAuthError` with a sanitized
message. Storage callback exceptions retain their original type. Do not put
credentials into application exception messages or logs.

HTTPS is required for MCP and OAuth endpoints. For local development,
`allow_http=True` permits HTTP only on loopback hosts. HTTP loopback callback
URIs are accepted independently of this setting. Redirects on credentialed
requests are rejected.

For applications that accept untrusted server URLs, use the async
`check_url(url)` callback and network egress controls to enforce permitted
destinations. HTTPS validation alone does not prevent access to private
networks. The library permits corporate HTTPS authorization servers on private
networks and does not impose a public-address-only policy.
