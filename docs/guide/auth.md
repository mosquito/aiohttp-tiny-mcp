# Authentication

Pass an `Authentication` subclass to `Registry(auth=...)`. The policy receives
the HTTP request and returns a verified `Principal`. Both `Endpoint` and
`SseEndpoint` use the same policy. The endpoints check expiry and required
scopes, inject the principal into handlers, and enforce session ownership.

## Choose an integration

| Requirement | Integration | Application code |
| --- | --- | --- |
| One Basic account | `StaticBasicAuth` | Configure the account and scopes. |
| Basic with a user database | Subclass `BasicAuth` | Implement async `verify(username, password)`. |
| API key, cookie, or existing request identity | Subclass `Authentication` | Implement async `authenticate(request)` and `challenge(refusal)`. |
| Existing OAuth Bearer tokens | `Authorization` | Supply an object with async `verify(token)`. Inheritance is optional. |
| GitHub browser login | `OAuthFacade` with `GitHub` | Supply `identity=` to map verified accounts to permissions. |
| Another OAuth provider | `OAuth2` with `OAuthFacade` | Configure endpoints and an async `profile(tokens, http)` callback. |

Use [Basic authentication](#basic-authentication) or
[custom authentication](#custom-authentication) for subclass examples.
[GitHub sign-in](#github-sign-in) covers application registration and console setup.
[OAuth provider adapters](#oauth-provider-adapters) covers other providers.
[Mapping accounts and roles](#mapping-accounts-and-roles) explains permission mapping;
[handler permissions](#handler-permissions) covers operation and record checks.
See [Token lifetime and refresh](#token-lifetime-and-refresh) for current lifecycle support.

## Basic authentication

Use `StaticBasicAuth` for one configured account:

<!-- name: async test_basic_auth -->
```python
from aiohttp_tiny_mcp import Registry, StaticBasicAuth

auth = StaticBasicAuth("alice", "example-password", realm="reports", scopes=["reports:read"])
registry = Registry("reports", "1.0", auth=auth)
```

Clients send `Authorization: Basic <base64(username:password)>`. The policy
expects UTF-8 and returns a Basic challenge on rejection. Use HTTPS: Base64
does not encrypt credentials. See [RFC 7617](https://www.rfc-editor.org/rfc/rfc7617.html).
Basic authentication publishes no OAuth metadata and does not implement OAuth
login. Client support for Basic authentication is required.

For a user database or another credential source, subclass `BasicAuth` and
implement only `verify`. It receives the decoded username and password:

<!-- name: async test_basic_auth -->
```python
from secrets import compare_digest

from aiohttp.test_utils import make_mocked_request

from aiohttp_tiny_mcp import BasicAuth, Principal


class Users(BasicAuth):
    async def verify(self, username: str, password: str) -> Principal | None:
        # Replace this example comparison with your user store's password check.
        if username == "alice" and compare_digest(password.encode(), b"example-password"):
            return Principal(
                subject="alice", namespace="reports", scopes=frozenset({"reports:read"})
            )
        return None


registry = Registry("reports", "1.0", auth=Users(realm="reports"))
request = make_mocked_request(
    "GET", "/mcp", headers={"Authorization": "Basic YWxpY2U6ZXhhbXBsZS1wYXNzd29yZA=="}
)
principal = await registry.auth.authenticate(request)
assert principal is not None and principal.subject == "alice"
```

Basic policies read the Authorization header only. They reject malformed
Base64, invalid UTF-8, missing separators, and control characters. Passwords
may contain colons; usernames cannot. `StaticBasicAuth` rejects empty configured
credentials and keeps the password out of its object representation.

## Custom authentication

Subclass `Authentication` when you need control over credential extraction
and verification. Implement `authenticate(request)` and `challenge(refusal)`:

<!-- name: async test_custom_auth -->
```python
from secrets import compare_digest

from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from aiohttp_tiny_mcp import Authentication, Principal, Registry, Unauthorized


class ApiKey(Authentication):
    def __init__(self, secret: str):
        super().__init__()
        self.secret = secret.encode()

    async def authenticate(self, request: web.Request) -> Principal | None:
        supplied = request.headers.get("X-API-Key", "").encode()
        if compare_digest(supplied, self.secret):
            return Principal(subject="service", namespace="reports")
        return None

    def challenge(self, refusal: Unauthorized) -> str:
        return 'ApiKey realm="reports"'


policy = ApiKey("example-secret")
registry = Registry("reports", "1.0", auth=policy)
request = make_mocked_request("POST", "/mcp", headers={"X-API-Key": "example-secret"})
assert policy.check(await policy.authenticate(request)).subject == "service"
```

Return `None` for invalid credentials, or raise `Unauthorized` with a specific
error, description, and HTTP status. Unexpected exceptions are not treated as
authentication failures for another policy to bypass. A subclass can inspect
headers, query parameters, cookies, or trusted middleware state. It must verify
those values before constructing a principal. Custom policies publish no
metadata unless they override `metadata_path` and `metadata()`.

Call `super().__init__(required_scopes=[...])` in a custom constructor to configure
endpoint-wide permissions. The same constructor accepts `bind_sessions` and
`namespace_from_token`; keep their defaults unless another trusted layer owns
session isolation. The endpoint calls `check()` after `authenticate()`; you do
not need to call it inside the subclass. `challenge()` returns only the header
value, without `WWW-Authenticate:`. It is synchronous.

Use the same policy instance for the endpoint lifetime. Keep request-specific
identity in the returned `Principal`, not on the policy instance. Concurrent
requests share the instance. Inject your database or service into its constructor;
manage that dependency's lifecycle through your aiohttp application.

Authentication protects the MCP routes. It does not protect unrelated routes
or the static console; configure application middleware for those routes if needed.
The HTTP policies do not authenticate stdio connections.

## Multiple authentication methods

`auth=` accepts one policy or an iterable. Iterables are consumed once;
an empty iterable is rejected. Use `auth=None` for an unprotected endpoint.

<!-- name: async test_basic_auth -->
```python
from aiohttp_tiny_mcp import Authorization


class Tokens:
    async def verify(self, token: str) -> Principal | None:
        if token == "example-token":
            return Principal(
                subject="alice", namespace="reports", scopes=frozenset({"reports:read"})
            )
        return None


registry = Registry(
    "reports",
    "1.0",
    auth=[
        Users(realm="reports"),
        Authorization(Tokens(), resource="https://mcp.example.com/mcp"),
    ],
)
assert len(registry.auth_policies) == 2
```

Policies are alternatives, evaluated in registration order. The first policy
that returns a valid principal with its required scopes succeeds. Claims and
scopes from different policies are never combined. If every policy rejects
the request, the response advertises all challenges. A missing-scope refusal
takes precedence over an invalid-credential refusal. Only policies that declare
metadata add routes; equal documents at one path share a route, and conflicting
documents cause a configuration error.

## Principal and storage namespaces

| Field | Purpose |
| --- | --- |
| `subject` | Stable application account ID. |
| `issuer` | Identity authority; participates in session ownership. |
| `client_id` | Calling application's ID; identifies the owner when `subject` is absent. |
| `scopes` | Verified permission strings; there is no implicit role hierarchy or wildcard expansion. |
| `claims` | Application data for handlers, such as an organization ID. Claims grant no permissions automatically. |
| `namespace` | Storage and notification partition, such as a verified organization ID. |
| `expires_at` | Expiry as Unix time in seconds, or `None` for no principal-level expiry. |

`Principal.namespace` selects the Hub and session storage namespace. When it
is `None`, the library uses `Principal.identity`. Trusted middleware namespaces
take precedence, and `namespace_from_token=False` on the selected policy disables
automatic namespace selection. This setting applies to Basic and custom policies
as well as tokens. `bind_sessions` also comes from the selected policy.

Use the same verified namespace for accounts that must share notifications.
Session ownership still compares `Principal.identity`, so a shared namespace
does not grant access to another user's session. Two authentication methods
can access the same session only when they resolve to the same identity and
storage namespace. Choose stable identities; do not use passwords or tokens
as identity or namespace values. These namespaces do not add application-level
permissions for projects or database records.

For a subject, `identity` is `"<issuer>|<subject>"`; a missing issuer becomes an
empty string. For a client without a subject, it is `"<issuer>|client:<client_id>"`.
Matching usernames alone do not unify identities across issuers. The OAuth facade
sets the issuer to its configured URL and the client ID to the registered MCP client.

## Handler permissions

Annotate a handler parameter with `Principal` to receive the verified identity.
It is injected by the server and does not appear in the input schema.
`required_scopes` applies to every request accepted by that policy. Missing
required scopes produce HTTP 403 before dispatch.

`@registry.tool(scopes=[...])` checks the verified principal before calling the
tool. A missing tool scope returns an error tool result. Resource, prompt,
and extension handlers must implement their own operation-specific checks;
they can also receive `Principal` through dependency injection. Application
permissions, such as access to one project or database row, remain application
logic. Client-reported names and capabilities are not verified identities.

`principal.holds(wanted)` returns the **missing scopes**, not a boolean indicating
success. An empty set means the principal holds every requested scope:

<!-- name: async test_record_permissions -->
```python
from pydantic import BaseModel

from aiohttp_tiny_mcp import CallToolResult, Principal, Registry, StaticBasicAuth

registry = Registry(
    "reports",
    "1.0",
    auth=StaticBasicAuth("alice", "example-password", scopes=["reports:read"]),
)
REPORTS = {"quarterly": {"owner": "alice", "text": "Quarterly results"}}


class ReportRequest(BaseModel):
    report_id: str


@registry.tool(scopes=["reports:read"])
async def read_report(args: ReportRequest, principal: Principal) -> str | CallToolResult:
    report = REPORTS.get(args.report_id)
    if report is None or report["owner"] != principal.subject:
        return CallToolResult.failure("Report not found or access denied")
    return report["text"]


alice = Principal(subject="alice", scopes=frozenset({"reports:read"}))
bob = Principal(subject="bob", scopes=frozenset({"reports:read"}))
assert not alice.holds(["reports:read"])
assert alice.holds(["reports:write"]) == frozenset({"reports:write"})
assert await read_report(ReportRequest(report_id="quarterly"), alice) == "Quarterly results"
assert await read_report(ReportRequest(report_id="quarterly"), bob) == CallToolResult.failure(
    "Report not found or access denied"
)
```

The dispatcher checks the tool scope; the handler checks ownership against
server-side data. Calling the Python function directly bypasses dispatcher scope
checks. For resources, prompts, and extensions, explicitly check `holds()` before
reading or changing protected data. Scope checks do not hide tool definitions
from the catalogue.

## Sending credentials

Configure headers on the `aiohttp.ClientSession` passed to `Client`:

<!-- name: async test_auth_client; fixtures: serve -->
```python
from aiohttp import ClientSession, encode_basic_auth

from aiohttp_tiny_mcp import Client, Registry, StaticBasicAuth
from aiohttp_tiny_mcp.protocol.selection import AdapterSet

registry = Registry("reports", "1.0", auth=StaticBasicAuth("alice", "example-password"))
url = await serve(registry)
headers = {"Authorization": encode_basic_auth("alice", "example-password")}
async with ClientSession(headers=headers) as http:
    adapter = AdapterSet.default().by_version["2026-07-28"]
    async with Client(url, adapter, session=http) as client:
        await client.initialize()
```

For Bearer authentication, use `{"Authorization": "Bearer <access-token>"}`.
The client sends the configured headers on its HTTP requests. The client does
not obtain or refresh OAuth tokens automatically. Basic and Bearer policies
do not read credentials from URLs; use a custom policy when required by your
application.

Legacy HTTP+SSE clients must authenticate both `GET /sse` and every
`POST /messages`. A successful stream opening does not authenticate later
POSTs. Missing or invalid credentials return HTTP 401 with the configured
challenge, before creating a session or dispatching a message. A different
session owner receives HTTP 404. Older clients need a separately acquired
token if they lack OAuth discovery and login support.

## Console authentication

Mount `Console` normally. `Registry(auth=...)` protects MCP requests while the
console HTML, JavaScript, and CSS remain public. The page first connects without
credentials. An HTTP 401 opens the authentication dialog. The console reads
`WWW-Authenticate`, which the endpoint builds from each policy's `challenge()`.
This uses [HTTP authentication challenges](https://www.rfc-editor.org/rfc/rfc9110.html#section-11.6.1)
and works with every supported MCP revision.

Select **Basic** and enter a username and password, or select **Bearer token**
and enter an existing access token. With the OAuth configuration below, use
**Sign in with OAuth** to obtain an MCP token through browser login.
Multiple configured policies appear in the
server authentication message. Use **Authentication** to change credentials;
**Connect** starts a new MCP session. **Clear** removes credentials and disconnects.
Closing the dialog cancels editing without changing active credentials.

Credentials stay in page memory. The console sends them on all MCP requests,
including notifications and answers to server questions. It does not put them
in browser storage, URLs, or the traffic panel. Editing the endpoint or reloading
the page clears them. Disconnecting retains them for reconnecting to the same
endpoint. Credentialed requests do not follow redirects. Cookies and browser-managed
HTTP credentials are not sent.

For a custom policy, use **Custom header** with `Authorization` or an `X-*`
header, such as `X-API-Key`, and its complete value. A challenge cannot describe
arbitrary credential fields, so the user supplies the header name. The console
does not implement Digest, cookie login, or token refresh. OAuth protected-resource
metadata remains available through
`Authorization`; it does not define a Basic login form. No additional discovery
endpoint is required for the console.

### Application middleware and public assets

Application-wide authentication middleware must explicitly allow console assets.
Use `Console.is_public(request)` before its existing authentication checks:

<!-- name: async test_public_console_middleware -->
```python
from aiohttp import web

from aiohttp_tiny_mcp.console import Console


def allow_console(authentication_middleware):
    @web.middleware
    async def middleware(request, handler):
        if Console.is_public(request):
            return await handler(request)
        return await authentication_middleware(request, handler)

    return middleware
```

Install `allow_console(your_authentication_middleware)` in the application's
middleware list. The check uses the resolved console handler and the asset
allowlist. It permits only GET and HEAD. It supports root mounts, custom prefixes,
and subapplications. An unmounted console grants no exemptions. Other routes,
unknown files, asset path suffixes, and unsupported methods remain protected.
Console setup cannot bypass arbitrary application middleware or proxy rules.

For Backlog, replace the `CONSOLE_ASSETS` path check with
`Console.is_public(request)`. Keep its existing checks for all other requests.
Mount the console only when enabled; no separate disabled-console exception is
needed. Middleware must return the appropriate `WWW-Authenticate` challenge on
rejection so the dialog can select Basic or Bearer automatically.

## OAuth provider adapters

Provider integration has two independent callbacks:

1. `OAuth2.profile(tokens, http)` calls the provider and returns a verified `Identity`.
2. `OAuthFacade(identity=...)` maps that identity to an application `Principal`.

Keep provider response parsing in the first callback and application permissions
in the second. The same mapper can serve multiple providers by looking up
`(identity.provider, identity.sub)`.

For a provider with an authorization-code flow, PKCE S256, and a JSON profile API,
configure `OAuth2` directly. This example expects a profile object with a stable
string `id` and an optional `display_name`:

<!-- name: async test_custom_oauth_provider -->
```python
import asyncio

from aiohttp import ClientError, ClientSession

from aiohttp_tiny_mcp.oauth import Identity, OAuth2, UpstreamAuthError, UpstreamTokens


async def company_profile(tokens: UpstreamTokens, http: ClientSession) -> Identity:
    try:
        async with http.get(
            "https://identity.example.com/api/me",
            headers={"Authorization": f"Bearer {tokens.access_token}"},
            allow_redirects=False,
        ) as response:
            if response.status != 200:
                raise UpstreamAuthError("The provider refused the profile request.")
            profile = await response.json()
    except (ClientError, asyncio.TimeoutError, ValueError) as error:
        raise UpstreamAuthError("The provider profile is unavailable.") from error
    if not isinstance(profile, dict) or not isinstance(profile.get("id"), str):
        raise UpstreamAuthError("The provider returned an invalid account ID.")
    if not profile["id"]:
        raise UpstreamAuthError("The provider returned an empty account ID.")
    return Identity(
        sub=profile["id"],
        provider="company",
        claims={"display_name": profile.get("display_name")},
    )


provider = OAuth2(
    name="Company login",
    authorize_url="https://identity.example.com/oauth/authorize",
    token_url="https://identity.example.com/oauth/token",
    client_id="YOUR_CLIENT_ID",
    client_secret="YOUR_CLIENT_SECRET",
    scopes=["profile"],
    profile=company_profile,
)
assert provider.profile is company_profile
```

Pass this object as `upstream=provider` to `OAuthFacade`. Configure the facade,
registered MCP clients, and console as in the GitHub example below. The facade
owns the HTTP session passed to `profile`; do not close it. Raise
`UpstreamAuthError` for expected provider failures. Its internal error text is
not sent to the browser. Return only verified identity data, never credentials.

The default exchange sends the client ID and secret in form data. It expects a
JSON response with a Bearer access token and accepts optional `expires_in` and
`refresh_token` fields. Providers that require different token authentication
or response formats need an `OAuth2` subclass with these extension points:

| Extension point | Contract |
| --- | --- |
| `authorization_url(*, redirect_uri, state, challenge) -> str` | Build the provider redirect. Preserve state, the exact callback, and PKCE S256. |
| `async exchange(http, *, code, redirect_uri, verifier) -> UpstreamTokens` | Exchange the code using the provider's required authentication and parse its response. |
| `profile(tokens, http)` constructor argument | Fetch and validate the account, then return `Identity`. |
| `extra_authorize_params` constructor argument | Add provider-specific authorization parameters; reserved OAuth parameters retain the library's values. |

These hooks adapt an authorization-code provider. The library does not currently
supply OIDC discovery or ID-token validation, SAML, device flow, or a general
refresh interface. An OIDC integration must validate its ID token with an OIDC
implementation; decoding its payload does not verify identity. For an existing
application login or another credential scheme, use
[custom authentication](#custom-authentication) instead.

## GitHub sign-in

Use `OAuthFacade` with the provider from `aiohttp_tiny_mcp.oauth.github` to sign
in through a GitHub OAuth App. The facade acts as the authorization server for
MCP. GitHub supplies the identity. The facade issues a separate, short-lived
MCP token; it never accepts a GitHub access token as an MCP credential.

Create an OAuth App in GitHub and set its **Authorization callback URL** to
`https://mcp.example.com/oauth/callback`. Load the app's client ID and secret
from your deployment configuration. These credentials belong to the server.
Never include the client secret in console HTML or JavaScript.

This example permits one GitHub account, identified by its numeric user ID:

<!-- name: async test_github_oauth_configuration -->
```python
from aiohttp_tiny_mcp import Endpoint, Principal, Registry
from aiohttp_tiny_mcp.console import Console
from aiohttp_tiny_mcp.oauth import Identity, OAuthClient, OAuthFacade
from aiohttp_tiny_mcp.oauth.github import GitHub


def allow_user(identity: Identity) -> Principal | None:
    if identity.provider != "github" or identity.sub != "1234567":
        return None
    return Principal(
        subject=f"github:{identity.sub}",
        scopes=frozenset({"reports:read"}),
        claims=identity.claims,
    )


oauth = OAuthFacade(
    issuer="https://mcp.example.com/oauth",
    upstream=GitHub(client_id="YOUR_CLIENT_ID", client_secret="YOUR_CLIENT_SECRET"),
    resource="https://mcp.example.com/mcp",
    clients=[
        OAuthClient(
            client_id="console",
            client_name="Reports console",
            redirect_uris=["https://mcp.example.com/console/oauth-callback"],
        )
    ],
    scopes=["reports:read"],
    identity=allow_user,
)
registry = Registry("reports", "1.0", auth=oauth.resource(required_scopes=["reports:read"]))
app = Endpoint(registry).app("/mcp")
oauth.setup(app)
Console("/mcp", oauth_client_id="console").setup(app, "/console")
```

The two callback URLs have different roles. `/oauth/callback` receives the
GitHub response. `/console/oauth-callback` returns the MCP authorization code
to the console popup. Register other MCP clients with their own `OAuthClient`
IDs and exact callback URLs. Configure those clients with the same IDs.

The console reads the Bearer challenge, protected-resource metadata, and
authorization-server metadata. **Sign in with OAuth** opens a popup that redirects
straight to GitHub. The user signs in and returns to the console.
The console checks the response state and issuer before exchanging the code
with PKCE. The MCP token stays in page memory. Allow popups for this page.

Explicitly registered clients skip the local consent page by default. Register
only clients you trust. To require local confirmation for a client, set
`OAuthClient(..., require_consent=True)`. This setting does not bypass GitHub's
own consent screen. Both paths enforce PKCE, browser-bound state, and exact
callback validation.

`identity` can be synchronous or asynchronous. Return `None` to deny access,
or return a `Principal` with the account's allowed MCP scopes and namespace.
The facade rejects requests for scopes that the principal does not hold.
Without this callback, every successfully authenticated provider account is
allowed. GitHub identities use numeric IDs, so login renames do not change
session ownership. GitHub permissions (`read:user` by default) and MCP scopes
are separate configuration values.

Mount facade routes at the origin root, even when MCP is in a subapplication.
For the example issuer, metadata is served at
`/.well-known/oauth-authorization-server/oauth`. `issuer` and `resource` are
explicit public URLs; the facade never derives them from a request's Host header.
HTTPS is required, with HTTP permitted for loopback development addresses.
Use `OAuthFacade.is_public(request)` alongside `Console.is_public(request)` in
application-wide middleware. Facade routes enforce their own OAuth checks.
The protected-resource metadata route must also remain reachable without an
MCP token, as described below.

The default store is process-local. Pass `store=shared_session_store` and use
the same issuer, resource, and client registrations on every worker. The facade
uses a separate key prefix and atomic compare-and-swap for single-use codes.
Stored access tokens are indexed by a digest. `await oauth.revoke(token)` removes
a locally issued token. Configure `access_token_ttl` to set its lifetime; the
default is one hour, with a maximum of one day. Apply deployment rate limits to
login routes and bound storage capacity for the expected workload.

Current limits:

- Clients must be registered explicitly. CIMD and dynamic registration are not implemented.
- Only the authorization-code grant with PKCE S256 is implemented. Sign in again
  after an MCP token expires; MCP refresh tokens are not issued.
- The console supports OAuth metadata and token endpoints on its own origin.
- Provider access and optional refresh tokens are used for identity lookup and
  then discarded. GitHub API access and installation tokens are not exposed to tools.
- GitHub revocation or account changes do not invalidate an issued MCP token
  immediately. Its local expiry or explicit revocation controls its lifetime.
- The preset targets GitHub.com OAuth Apps. GitHub Enterprise and GitHub App
  installation flows need separate provider configuration.

GitHub supports PKCE and can return either non-expiring tokens or expiring tokens
with a refresh token. The provider handles both response shapes. See
[GitHub's OAuth App flow](https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/authorizing-oauth-apps)
for application registration and provider settings.

## Mapping accounts and roles

Use `OAuthFacade(identity=...)` to translate a verified provider identity into
your application's account. You do not need to subclass `GitHub` or `OAuthFacade`.
`GitHub` is a provider factory. The mapper accepts one `Identity` and returns a
`Principal` or `None`; an async mapper can query your account database.

This example uses server-owned account and role mappings. Unknown or disabled
accounts are denied. Replace the dictionary lookup with your database lookup:

<!-- name: async test_oauth_permission_mapping -->
```python
from aiohttp_tiny_mcp import Principal
from aiohttp_tiny_mcp.oauth import Identity

ROLE_SCOPES = {
    "reader": frozenset({"reports:read"}),
    "editor": frozenset({"reports:read", "reports:write"}),
}
ACCOUNTS = {
    ("github", "1234567"): {
        "id": "account-42",
        "organization": "acme",
        "role": "editor",
        "enabled": True,
    },
    ("github", "7654321"): {
        "id": "account-43",
        "organization": "acme",
        "role": "reader",
        "enabled": False,
    },
}


async def map_identity(identity: Identity) -> Principal | None:
    account = ACCOUNTS.get((identity.provider, identity.sub))
    if account is None or not account["enabled"]:
        return None
    scopes = ROLE_SCOPES.get(account["role"])
    if scopes is None:
        return None
    return Principal(
        subject=account["id"],
        namespace=f"organization:{account['organization']}",
        scopes=scopes,
        claims={"organization": account["organization"], "role": account["role"]},
    )


principal = await map_identity(Identity(sub="1234567", provider="github"))
assert principal is not None and principal.subject == "account-42"
assert not principal.holds(["reports:write"])
assert principal.namespace == "organization:acme"
assert await map_identity(Identity(sub="7654321", provider="github")) is None
assert await map_identity(Identity(sub="unknown", provider="github")) is None
```

Pass `identity=map_identity` to the earlier `OAuthFacade` configuration. Declare
the available MCP scopes with `scopes=["reports:read", "reports:write"]`.
Use `oauth.resource(required_scopes=["reports:read"])` for endpoint access, and
`@registry.tool(scopes=["reports:write"])` for tools that change reports.

The permission layers have separate roles:

| Configuration | Meaning |
| --- | --- |
| `GitHub(scopes=["read:user"])` | Permissions requested from GitHub for provider API access. |
| `OAuthFacade(scopes=[...])` | MCP scope names the authorization server accepts. |
| Mapper's `Principal.scopes` | Maximum MCP permissions allowed for this account. |
| Client's OAuth `scope` parameter | Permissions requested for this MCP token. |
| `required_scopes` / tool `scopes` | Permissions required to accept a request or call a tool. |

The facade rejects a request if any requested scope is unavailable or exceeds
the mapped account's permissions. It does not silently reduce the request.
The issued principal contains the requested scopes, which can be fewer than
the mapper returned. Omitting `scope` requests every scope configured on the facade.
The console currently requests every scope advertised by the protected resource;
it has no scope selector. With the two-scope configuration above, a reader cannot
complete console login. A client that requests only `reports:read` can log in.

The mapper receives the verified profile, not the provider token. The GitHub
preset supplies `login`, `name`, and `email` claims when available; it does not
load organization membership, teams, or repository permissions. Use your account
database for application roles. For extra provider lookups, supply a custom
`OAuth2.profile` callback that returns `Identity`, then apply the same mapper.
Keep credentials out of claims; facade principals must be JSON-serializable.

Mapping runs at login. The facade stores the resulting principal with the MCP
token; later account or role changes do not automatically update that token.
Use a short `access_token_ttl` or `await oauth.revoke(token)` for known tokens.
For immediate account or record restrictions, check current application data in
handlers or an authentication policy on each request. The facade sets token expiry
to the earlier of its configured lifetime and the mapper's `expires_at`.

## Token lifetime and refresh

MCP tokens and provider tokens have separate issuers, consumers, and lifetimes.
A refresh token is optional; when issued, it is exchanged at the issuing server's
token endpoint. See [RFC 6749, section 6](https://www.rfc-editor.org/rfc/rfc6749#section-6).

| Credential | Current library behavior |
| --- | --- |
| MCP access token issued by `OAuthFacade` | Stored under a digest, checked on each MCP HTTP request, rejected after expiry or local revocation. |
| MCP refresh token | Not issued or accepted. The console and Python client do not refresh tokens. Sign in again after expiry. |
| Provider access token | Used during the callback to fetch identity, then discarded. No periodic checks or retention for later tool calls. |
| Provider refresh token | Parsed when present, then discarded with the provider access token. No refresh request is made. |
| Token verified by an application `Authorization` verifier | The verifier runs per request. Its implementation owns signature checks, introspection, revocation checks, and any caching. |

For login-only integrations, retaining and refreshing provider tokens is unnecessary
after identity lookup. The MCP session then follows the application's own expiry
and revocation policy. Checking local expiry does not detect upstream revocation
or changed roles; the facade does not poll the provider or rerun its mapper.

If tools must call the provider later, the application currently needs a separate
credential store and token manager. Check expiry before use and refresh shortly
before expiry when supported. A fixed background interval alone cannot guarantee
that a token remains usable between checks. Revalidate account permissions using
current application data or a provider-supported check; refreshing a token alone
does not update the stored MCP principal.

Coordinate refresh across workers and atomically store rotated refresh tokens.
Treat a rejected refresh grant as requiring sign-in; distinguish it from temporary
network failures. Avoid replaying an entire state-changing tool merely because
token renewal failed. Refresh-token protection and rotation requirements are
described in [RFC 9700, section 4.14](https://www.rfc-editor.org/rfc/rfc9700#section-4.14).
This lifecycle is application-owned today; `UpstreamTokens` is a response model,
not an automatically refreshed tool dependency.

## OAuth Bearer authentication

```{mermaid}
flowchart TD
    start[Where does MCP run?]
    start --> facade[MCP facade over an existing API]
    start --> embedded[MCP inside your aiohttp application]
    facade --> oauth[OAuth protected resource]
    oauth --> metadata[Authorization publishes metadata and verifies Bearer tokens]
    embedded --> middleware[Application middleware]
    middleware --> jwt[Middleware verifies JWT and injects the application user]
```

Use `Authorization` when this MCP endpoint is an OAuth protected resource.
Your application supplies token verification through JWT validation,
introspection, or `OAuthFacade.resource()`. `Authorization` itself does not
run an authorization server or register clients.

Give resource the public MCP URL, including its path. Endpoint publishes RFC
9728 protected-resource metadata below its well-known path and returns a Bearer
challenge for missing, invalid, expired, or insufficient tokens.
authorization_servers tells an MCP client where it may obtain a token.

<!-- name: async test_auth -->
```python
from pydantic import BaseModel

from aiohttp_tiny_mcp import Registry
from aiohttp_tiny_mcp.auth import Authorization, Principal


class Tokens:
    async def verify(self, token: str) -> Principal | None:
        if token == "report-token":
            return Principal(
                subject="alice",
                client_id="reports",
                issuer="https://login.example.com",
                scopes=frozenset({"reports:read"}),
            )
        return None


auth = Authorization(
    verifier=Tokens(),
    resource="https://mcp.example.com/reports/mcp",
    authorization_servers=["https://login.example.com"],
    scopes_supported=["reports:read"],
)
registry = Registry("reports", "1.0", auth=auth)


class Nothing(BaseModel):
    pass


@registry.tool(scopes=["reports:read"])
async def report(args: Nothing, principal: Principal) -> str:
    """Return the current caller's report."""
    return f"report for {principal.subject}"


principal = await auth.principal("Bearer report-token")
assert not principal.holds(["reports:read"])
assert await report(Nothing(), principal) == "report for alice"
assert auth.metadata()["resource"] == "https://mcp.example.com/reports/mcp"
```

Invalid tokens return 401; missing required scopes return 403. By default, the
verified principal's namespace (or identity when unset) separates sessions, subscriptions, and
pending questions. This prevents callers from sharing state. Set
namespace_from_token=False only when your application sets a namespace in its
own trusted middleware.

bind_sessions=True is also the default: a legacy HTTP session belongs to the
principal that opened it. Do not disable it unless a separate trusted layer
binds session IDs to callers.

These checks also apply to the legacy `SseEndpoint`: both `GET /sse` and
every `POST /messages` require the Bearer header. Verification occurs before
opening a stream or dispatching a message. The stored session owner is checked
on each POST, including requests handled by another worker. Historical clients
must acquire a token separately if they lack OAuth discovery and login support.
See [HTTP+SSE deployment](../deployment/transports.md#httpsse-for-2024-11-05-clients)
for mounting both transports with one metadata route.

## Where the metadata has to be served

The library logs a warning when `add_subapp()` adds a prefix to a `.well-known`
metadata route. The warning includes the required path and the prefixed path.
It applies to protected-resource metadata from both HTTP transports and to
authorization-server metadata from `OAuthFacade`. It also works with `routes()`
and `app.add_routes()`. The warning does not move routes or prevent startup.

An MCP path such as `/reports/mcp` on the root application is valid and does not
trigger this warning. Reverse-proxy path rewriting is outside this check; ensure
the proxy exposes the documented public metadata URLs.

Endpoint.routes includes the metadata route along with the endpoint, and
Endpoint.metadata_routes returns it alone. The path comes from resource, not
from where you mounted anything, and RFC 8615 places a well-known URI directly
under the authority. Give resource the endpoint's public URL, so the two agree.

`SseEndpoint.routes()` and `metadata_routes()` follow the same rule. When both
transports share one protected resource, pass `metadata=False` to one endpoint
to avoid registering the metadata route twice.

<!-- name: async test_auth -->
```python
assert auth.metadata_path == "/.well-known/oauth-protected-resource/reports/mcp"
assert auth.metadata_url == "https://mcp.example.com" + auth.metadata_path
```

A prefix in the path you pass to routes or setup is enough, and keeps the
metadata at the root. Mounting the endpoint inside add_subapp does not: aiohttp
prefixes every route a subapplication holds, including this one, and a client
never looks there. Pass metadata=False to routes and give metadata_routes to
the application that owns the root:

<!-- name: async test_auth -->
```python
from aiohttp import web

from aiohttp_tiny_mcp import Endpoint, SseEndpoint

endpoint = Endpoint(registry)
section = web.Application()
section.add_routes(endpoint.routes("/mcp", metadata=False))

root = web.Application()
root.add_subapp("/reports", section)
root.add_routes(endpoint.metadata_routes())

assert [resource.canonical for resource in root.router.resources()] == [
    "/reports",
    auth.metadata_path,
]
```

When both HTTP transports share a protected resource, publish its metadata once:

<!-- name: async test_auth -->
```python
both = web.Application()
Endpoint(registry).setup(both, "/mcp")
SseEndpoint(registry).setup(both, "/sse", "/messages", metadata=False)
```

`metadata_routes()` returns no routes for Basic policies or other policies
that declare no metadata. General mounting rules are in
[Transports](../deployment/transports.md#under-a-subapplication).

## JWT middleware in an existing aiohttp application

Use this path when the aiohttp application already owns login and access
policy. Do not pass auth= to Registry: the MCP endpoint then does not publish
OAuth resource metadata or enforce tool scopes itself. The application
middleware authenticates every request, and handlers receive the resulting
application user as an ordinary dependency.

Install [PyJWT](https://pyjwt.readthedocs.io/) as an application dependency.
The example uses a symmetric key only to keep the code small. With an external
issuer, validate a signature from its JWKS with a fixed algorithm, issuer, and
audience; never select the algorithm from the unverified token header.

<!-- name: async test_auth_jwt; fixtures: __name__ -->
```python
import jwt
from aiohttp import web
from aiohttp.test_utils import make_mocked_request
from pydantic import BaseModel

from aiohttp_tiny_mcp import CallToolResult, Endpoint, Registry
from aiohttp_tiny_mcp.namespaces import namespace


SECRET = "replace-this-example-secret-with-32-bytes"


class User:
    def __init__(self, subject: str, organization: str, scopes: frozenset[str]) -> None:
        self.subject = subject
        self.organization = organization
        self.scopes = scopes

    def holds(self, scope: str) -> bool:
        return scope in self.scopes


USER: web.RequestKey[User] = web.RequestKey("user", User)


@web.middleware
async def authenticate(request: web.Request, handler):
    header = request.headers.get("Authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise web.HTTPUnauthorized()
    try:
        claims = jwt.decode(
            token,
            SECRET,
            algorithms=["HS256"],
            audience="mcp-api",
            issuer="https://login.example.com",
        )
    except jwt.InvalidTokenError as error:
        raise web.HTTPUnauthorized(text="invalid token") from error

    user = User(
        claims["sub"],
        claims["organization"],
        frozenset(claims.get("scope", "").split()),
    )
    request[USER] = user
    reset = namespace.set(user.organization)
    try:
        return await handler(request)
    finally:
        namespace.reset(reset)


async def current_user(ex) -> User:
    return ex.request[USER]


registry = Registry("reports", "1.0")
registry.provide(User, current_user)


class Nothing(BaseModel):
    pass


@registry.tool
async def report(args: Nothing, user: User) -> str | CallToolResult:
    """Return a report in the caller's organization."""
    if not user.holds("reports:read"):
        return CallToolResult.failure("missing scope: reports:read")
    return f"report for {user.subject}"


app = web.Application(middlewares=[authenticate])
Endpoint(registry).setup(app, "/mcp")

token = jwt.encode(
    {
        "sub": "alice",
        "organization": "acme",
        "scope": "reports:read",
        "aud": "mcp-api",
        "iss": "https://login.example.com",
    },
    SECRET,
    algorithm="HS256",
)
request = make_mocked_request("POST", "/mcp", headers={"Authorization": f"Bearer {token}"})


async def next_handler(request):
    assert request[USER].subject == "alice"
    assert namespace.get() == "acme"
    return web.Response()


assert (await authenticate(request, next_handler)).status == 200
assert await report(Nothing(), User("alice", "acme", frozenset())) == CallToolResult.failure(
    "missing scope: reports:read"
)
```

The middleware must run before Endpoint. It should reject an unauthenticated
request before MCP parses it, and reset the namespace after the request. The
organization is a suitable namespace when every MCP session, subscription, and
pending question must stay inside that organization. Use the user subject
instead for per-user isolation.

JWT issuers commonly encode scopes as a space-separated scope claim; adapt the
extraction to your issuer if it uses a roles array or a custom claim. Unlike
Authorization, this application-owned route does not make tool(scopes=...)
automatic: check User.scopes in the handler, or enforce it in a provider for a
whole group of tools.

This application-owned route works over every transport that carries the
application identity. The bundled stdio transport has no HTTP Authorization
header, so use it only when the process launcher is the authentication boundary
and installs an equivalent trusted identity.
