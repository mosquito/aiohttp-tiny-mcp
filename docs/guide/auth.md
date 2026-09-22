# Authentication

Pass an authentication policy to `Registry(auth=...)`. HTTP and SSE endpoints
use its `Principal` for permissions, namespaces, and session ownership.
Custom authentication middleware is not required.

## Policies and token verifiers

`Authentication` is the policy base class. `Authorization` implements OAuth
Bearer authentication and delegates token verification to a `TokenVerifier`:

```{mermaid}
flowchart TD
    A["Authorization<br/>Extract the Bearer token"]
    A --> V["TokenVerifier<br/>Verify the token"]
    V --> P["Principal<br/>Identity and permissions"]
    P --> C["Authorization<br/>Check expiry and required scopes"]
    C --> E["Endpoint<br/>Select namespace and check session ownership"]
```

Replace the verifier to change token validation. Subclass `Authentication`
to change credential extraction, challenges, or metadata.

## Choose an integration

| Requirement | Integration |
| --- | --- |
| Fixed accounts or a user database | [Basic authentication](#basic-authentication) |
| API key, cookie, or existing identity | [Custom authentication](#custom-authentication) |
| Static tokens, JWT, or introspection | [OAuth Bearer authentication](#oauth-bearer-authentication) |
| GitHub browser login | [GitHub sign-in](#github-sign-in) |
| Another OAuth provider | [Provider adapters](#oauth-provider-adapters) |

See [permissions](#handler-permissions) and [token handling](#application-token-handling)
for access checks and refresh behavior.

## Basic authentication

Use `StaticBasicAuth` for one or more configured accounts with individual scopes:

<!-- name: async test_basic_auth -->
```python
from aiohttp_tiny_mcp import Registry, StaticBasicAuth

auth = StaticBasicAuth(
    ("alice", "alice-example-password", {"reports:read"}),
    ("bob", "bob-example-password", {"reports:read", "reports:write"}),
    realm="reports",
)
registry = Registry("reports", "1.0", auth=auth)
```

Each tuple holds `(username, password, scopes)`; omit scopes for an account
without permissions. Supply at least one account with unique usernames.
`required_scopes=` restricts access for all accounts.

Clients send UTF-8 credentials in the Basic Authorization header. Use HTTPS;
Base64 does not encrypt passwords. Basic authentication publishes no OAuth metadata.

For a user database, subclass `BasicAuth` and implement `verify`:

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

`StaticBasicAuth` rejects empty credentials. Custom `verify()` implementations
decide whether to accept them. Control characters are rejected; colons separate
the username from the password. Passwords may contain colons. Query credentials are not read.

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
Matching usernames alone do not unify identities across issuers. The OAuth server
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
    auth=StaticBasicAuth(("alice", "example-password", ["reports:read"])),
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

registry = Registry("reports", "1.0", auth=StaticBasicAuth(("alice", "example-password")))
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

## OAuth provider adapters

Provider integration has two independent callbacks:

1. `OAuthProvider.profile(tokens, http)` calls the provider and returns a verified `Identity`.
2. `OAuthServer(identity=...)` maps that identity to an application `Principal`.

Keep provider response parsing in the first callback and application permissions
in the second. The same mapper can serve multiple providers by looking up
`(identity.provider, identity.sub)`.

For a provider with an authorization-code flow, PKCE S256, and a JSON profile API,
configure `OAuthProvider` directly. This example expects a profile object with a stable
string `id` and an optional `display_name`:

<!-- name: async test_custom_oauth_provider -->
```python
import asyncio

from aiohttp import ClientError, ClientSession

from aiohttp_tiny_mcp.oauth import Identity, OAuthProvider, UpstreamAuthError


async def company_profile(tokens: dict, http: ClientSession) -> Identity:
    try:
        async with http.get(
            "https://identity.example.com/api/me",
            headers={"Authorization": f"Bearer {tokens['access_token']}"},
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


provider = OAuthProvider(
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

Pass this object as `provider=provider` to `OAuthServer`. Configure the server,
registered MCP clients, and console as in the GitHub example below. The server
owns the HTTP session passed to `profile`; do not close it. Raise
`UpstreamAuthError` for expected provider failures. Its internal error text is
not sent to the browser. Return only verified identity data, never credentials.

The provider validates its URLs and client ID. The default exchange sends the
client ID and optional `client_secret` in form data. It expects a
JSON response with a Bearer access token and accepts optional `expires_in` and
`refresh_token` fields. Providers that require different token authentication
or response formats need an `OAuthProvider` subclass with these extension points:

| Extension point | Contract |
| --- | --- |
| `authorization_url(*, redirect_uri, state, challenge) -> str` | Build the provider redirect. Preserve state, the exact callback, and PKCE S256. |
| `async exchange(http, *, code, redirect_uri, verifier) -> dict` | Exchange the code using the provider's required authentication and parse its response. |
| `profile(tokens, http)` constructor argument | Fetch and validate the account, then return `Identity`. |
| `extra_authorize_params` constructor argument | Add provider-specific authorization parameters; reserved OAuth parameters retain the library's values. |

These hooks adapt an authorization-code provider. The library does not currently
supply OIDC discovery or ID-token validation, SAML, device flow, or a general
refresh interface. An OIDC integration must validate its ID token with an OIDC
implementation; decoding its payload does not verify identity. For an existing
application login or another credential scheme, use
[custom authentication](#custom-authentication) instead.

## GitHub sign-in

Use `OAuthServer` with the provider from `aiohttp_tiny_mcp.oauth.github` to sign
in through a GitHub OAuth App. The server acts as the authorization server for
MCP. GitHub supplies the identity. The server issues a separate MCP token through
its default implementation or your token object. A GitHub token is not an MCP credential.

Create an OAuth App in GitHub and set its **Authorization callback URL** to
`https://mcp.example.com/oauth/callback`. Load the app's client ID and secret
from your deployment configuration. These credentials belong to the server.
Never include the client secret in console HTML or JavaScript.

This example permits one GitHub account, identified by its numeric user ID:

<!-- name: async test_github_oauth_configuration -->
```python
from aiohttp_tiny_mcp import Endpoint, Principal, Registry
from aiohttp_tiny_mcp.console import Console
from aiohttp_tiny_mcp.oauth import Identity, OAuthClient, OAuthServer
from aiohttp_tiny_mcp.oauth.github import GitHub


def allow_user(identity: Identity) -> Principal | None:
    if identity.provider != "github" or identity.sub != "1234567":
        return None
    return Principal(
        subject=f"github:{identity.sub}",
        scopes=frozenset({"reports:read"}),
        claims=identity.claims,
    )


oauth = OAuthServer(
    issuer="https://mcp.example.com/oauth",
    provider=GitHub(client_id="YOUR_CLIENT_ID", client_secret="YOUR_CLIENT_SECRET"),
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
The server rejects requests for scopes that the principal does not hold.
Without this callback, every successfully authenticated provider account is
allowed. GitHub identities use numeric IDs, so login renames do not change
session ownership. GitHub permissions (`read:user` by default) and MCP scopes
are separate configuration values.

Mount server routes at the origin root, even when MCP is in a subapplication.
For the example issuer, metadata is served at
`/.well-known/oauth-authorization-server/oauth`. `issuer` and `resource` are
explicit public URLs; the server never derives them from a request's Host header.
HTTPS is required, with HTTP permitted for loopback development addresses.
Use `OAuthServer.is_public(request)` alongside `Console.is_public(request)` in
application-wide middleware. OAuth routes enforce their own checks.
The protected-resource metadata route must also remain reachable without an
MCP token, as described below.

By default, access tokens are opaque, stored by digest, and valid for one hour.
Set `access_token_ttl` to change this, up to one day. `await oauth.revoke(token)`
revokes a default token. See [application token handling](#application-token-handling)
for custom formats and deployments on several computers.

Current limits:

- Clients must be registered explicitly. CIMD and dynamic registration are not implemented.
- Only the authorization-code grant with PKCE S256 is implemented. Sign in again
  after an MCP token expires; MCP refresh tokens are not issued.
- The console supports OAuth metadata and token endpoints on its own origin.
- Default `OpaqueTokens` discards provider credentials when issuing the MCP token.
  Provider tokens are not automatically injected into tools.
- GitHub revocation or account changes do not invalidate an issued MCP token
  immediately. Its local expiry or explicit revocation controls its lifetime.
- The preset targets GitHub.com OAuth Apps. GitHub Enterprise and GitHub App
  installation flows need separate provider configuration.

GitHub supports PKCE and can return either non-expiring tokens or expiring tokens
with a refresh token. The provider handles both response shapes. See
[GitHub's OAuth App flow](https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/authorizing-oauth-apps)
for application registration and provider settings.

## Mapping accounts and roles

Use `OAuthServer(identity=...)` to translate a verified provider identity into
your application's account. You do not need to subclass `GitHub` or `OAuthServer`.
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

Pass `identity=map_identity` to the earlier `OAuthServer` configuration. Declare
the available MCP scopes with `scopes=["reports:read", "reports:write"]`.
Use `oauth.resource(required_scopes=["reports:read"])` for endpoint access, and
`@registry.tool(scopes=["reports:write"])` for tools that change reports.

The permission layers have separate roles:

| Configuration | Meaning |
| --- | --- |
| `GitHub(scopes=["read:user"])` | Permissions requested from GitHub for provider API access. |
| `OAuthServer(scopes=[...])` | MCP scope names the authorization server accepts. |
| Mapper's `Principal.scopes` | Maximum MCP permissions allowed for this account. |
| Client's OAuth `scope` parameter | Permissions requested for this MCP token. |
| `required_scopes` / tool `scopes` | Permissions required to accept a request or call a tool. |

The server rejects a request if any requested scope is unavailable or exceeds
the mapped account's permissions. It does not silently reduce the request.
The issued principal contains the requested scopes, which can be fewer than
the mapper returned. Omitting `scope` requests every scope configured on the server.
The console currently requests every scope advertised by the protected resource;
it has no scope selector. With the two-scope configuration above, a reader cannot
complete console login. A client that requests only `reports:read` can log in.

The mapper receives the verified profile, not the provider token. The GitHub
preset supplies `login`, `name`, and `email` claims when available; it does not
load organization membership, teams, or repository permissions. Use your account
database for application roles. For extra provider lookups, supply a custom
`OAuthProvider.profile` callback that returns `Identity`, then apply the same mapper.
Keep credentials out of claims; server principals must be JSON-serializable.

Mapping runs at login. Later account or role changes do not automatically update
the issued token's permissions. Use a short `access_token_ttl`, or call
`await oauth.revoke(token)` when the token backend supports revocation.
`EncryptedTokens` does not support individual revocation.
For immediate account or record restrictions, check current application data in
handlers or an authentication policy on each request. The server sets token expiry
to the earlier of its configured lifetime and the mapper's `expires_at`.

## Application token handling

`OAuthServer` uses `OpaqueTokens` by default: revocable tokens stored by digest.
Pass `OpaqueTokens(issuer=..., resource=..., store=...)` to use a separate token
store. For tokens that carry encrypted provider credentials, use `EncryptedTokens`:

<!-- name: test_encrypted_tokens_configuration -->
```python
from aiohttp_tiny_mcp.oauth import EncryptedTokens, KECCAKCipher


def token_codec(saved_key: bytes) -> EncryptedTokens:
    return EncryptedTokens(
        KECCAKCipher(saved_key),
        issuer="https://mcp.example.com/oauth",
        resource="https://mcp.example.com/mcp",
    )
```

Pass `tokens=token_codec(saved_key)` to `OAuthServer`. Generate at least 32 random
key bytes once, then load the same saved key on each instance.
`KECCAKCipher` uses zlib compression, a random 16-byte nonce, SHAKE-256 XOR
encryption, and HMAC-SHA256. It needs no extra package or access-token database.
Its `compression_level` defaults to `9`; use `0` to disable compression.
Supply another `AbstractCipher` to change encryption. Its `encode(dict) -> bytes`
and `decode(bytes) -> dict` own serialization and integrity checks; invalid blobs
must raise `ValueError`. `EncryptedTokens` handles base64 and issuer/resource/expiry checks.

`tokens.decode(token)["upstream"]` retrieves the provider dictionary in server
code after those checks; invalid tokens raise `ValueError`. Individual revocation
is unavailable. Replacing the key invalidates all tokens issued with the old key.

Pass `tokens=your_object` to `OAuthServer`. It needs two async methods; no base
class or token model is required:

| Method | Contract |
| --- | --- |
| `issue(principal, upstream) -> str` | Receive the approved MCP principal and the complete provider response as a dictionary. Return your MCP bearer token. |
| `verify(token) -> Principal \| None` | Verify your token's integrity, issuer, resource and expiry. Return its principal, or `None` when invalid. |

Your object can save credentials in PostgreSQL, or return an AES-encrypted,
base64-encoded payload. The library does not select your storage or encryption
format. Keep provider credentials out of `Principal` and tool responses.
An optional `async revoke(token)` enables `oauth.revoke(token)`; without it,
that call raises `NotImplementedError`.

The callback stores the approved principal and provider response with the
authorization code for at most five minutes. This private flow store contains
provider credentials. `issue()` runs only after PKCE verification and atomic
code redemption. An abandoned login issues no token. If issuance fails, the
client must restart login; the code has already been consumed.
Honor the supplied principal's expiry and scopes. The server uses that expiry
in the token endpoint response and also rejects expired or wrong-issuer principals.

For several computers, configure the same public issuer, resource, clients and
provider credentials on each instance:

| State | What instances must share |
| --- | --- |
| Pending login and authorization codes | `store=` backed by Redis or PostgreSQL, with atomic compare-and-swap. |
| Opaque tokens | The token object's store; defaults to the server's `store=`. |
| Application tokens | Your database, or your encryption keys and verification settings for self-contained tokens. |

The default memory store serves one process. SQLite can share state between
processes on one computer. OAuth records have their own TTL and are independent
of MCP session closure. Self-contained tokens still need shared pending-login
state for this authorization-code flow.

MCP refresh tokens are not implemented. Provider refresh and retention belong
to your token object. Coordinate refresh across instances and atomically replace
rotated refresh tokens; see [RFC 6749, section 6](https://www.rfc-editor.org/rfc/rfc6749#section-6).
Refreshing provider tokens does not update MCP permissions. The account mapper
runs at login; use current application data when permissions need revalidation.

## OAuth Bearer authentication

Use `Authorization` when this MCP endpoint is an OAuth protected resource.
Your application supplies token verification through JWT validation,
introspection, or `OAuthServer.resource()`. `Authorization` itself does not
run an authorization server or register clients.

Give resource the public MCP URL, including its path. Endpoint publishes RFC
9728 protected-resource metadata below its well-known path and returns a Bearer
challenge for missing, invalid, expired, or insufficient tokens.
authorization_servers tells an MCP client where it may obtain a token.

<!-- name: async test_auth -->
```python
from pydantic import BaseModel

from aiohttp_tiny_mcp import Registry
from aiohttp_tiny_mcp.auth import Authorization, Principal, StaticVerifier


verifier = StaticVerifier(
    {
        "report-token": Principal(
            subject="alice",
            client_id="reports",
            issuer="https://login.example.com",
            scopes=frozenset({"reports:read"}),
        ),
    }
)


auth = Authorization(
    verifier=verifier,
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

### Static tokens and custom verifiers

`StaticVerifier(mapping)` maps non-empty tokens to principals for development
and tests, without extra dependencies.

Custom verifiers implement `async verify(token: str) -> Principal | None`;
return `None` for invalid tokens. Keep token parsing inside the verifier and
credentials out of `Principal`.

### JWT verifiers

Install the optional dependency:

```bash
pip install 'aiohttp-tiny-mcp[jwt]'
```

| Verifier | Key |
| --- | --- |
| `HMACJWTVerifier` | HS256 secret, at least 32 bytes |
| `PublicKeyJWTVerifier` | PEM public key or file: RSA ≥2048 bits, EC P-256/P-384/P-521, or EdDSA |

Both require `exp` and validate signatures and time claims. Algorithms follow
the configured key. Set `issuer=` and `audience=` explicitly: empty defaults
disable those checks, and `Authorization.resource` does not configure them.
Audience lists are supported. JWKS fetching and discovery are not included.
Core imports and claims mapping work without `[jwt]`.

<!-- name: async test_jwt_verifiers -->
```python
import time

import jwt

from aiohttp_tiny_mcp import Authorization
from aiohttp_tiny_mcp.auth.jwt import HMACJWTVerifier

secret = "replace-this-example-secret-with-random-bytes"
verifier = HMACJWTVerifier(
    secret,
    issuer="https://login.example.com",
    audience="https://mcp.example.com/mcp",
)
auth = Authorization(verifier, resource="https://mcp.example.com/mcp")
token = jwt.encode(
    {
        "sub": "alice",
        "iss": "https://login.example.com",
        "aud": "https://mcp.example.com/mcp",
        "scope": "reports:read",
        "exp": time.time() + 300,
    },
    secret,
    algorithm="HS256",
)
principal = await auth.principal(f"Bearer {token}")
assert principal.subject == "alice"
assert principal.scopes == frozenset({"reports:read"})
```

### Map verified claims to a principal

Both JWT verifiers accept a synchronous `principal(claims) -> Principal`
callback, called after verification. Raise `ValueError` to reject the account.
The default `principal_from_claims` maps:

| Claim | Principal field | Default |
| --- | --- | --- |
| `sub` | `subject` | `None` |
| `iss` | `issuer` | `None` |
| `client_id`, otherwise `azp` | `client_id` | Empty string |
| `scope`, otherwise `scp` | `scopes` | Empty frozenset |
| `exp` | `expires_at` | `None` |

Scopes accept a space-separated string or a list; `scope` takes precedence
over `scp`, including when empty. Claims are copied to `Principal.claims`;
malformed identity, scope, and expiry values are rejected.

The mapper does not verify tokens. Empty claims produce `Principal()`;
separate session owners need a stable subject or client ID. Namespace defaults
to `None`. Override it through a custom mapper:

<!-- name: async test_jwt_verifiers -->
```python
from collections.abc import Mapping
from dataclasses import replace
from typing import Any

from aiohttp_tiny_mcp import Principal, principal_from_claims


def tenant_principal(claims: Mapping[str, Any]) -> Principal:
    principal = principal_from_claims(claims)
    if claims.get("org") != "example-team":
        raise ValueError("organization is not allowed")
    return replace(principal, namespace="example-team")


verifier = HMACJWTVerifier(
    secret,
    issuer="https://login.example.com",
    audience="https://mcp.example.com/mcp",
    principal=tenant_principal,
)
assert await verifier.verify(token) is None  # This token has no allowed organization.
assert principal_from_claims({}) == Principal()
```

Use `namespace=""` for shared storage. Session ownership still uses the
issuer-qualified subject or client ID, independently of namespace.

## Where the metadata has to be served

The library logs a warning when `add_subapp()` adds a prefix to a `.well-known`
metadata route. The warning includes the required path and the prefixed path.
It applies to protected-resource metadata from both HTTP transports and to
authorization-server metadata from `OAuthServer`. It also works with `routes()`
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

## Optional application middleware

To manage authentication entirely in your application, omit `Registry.auth`
and use aiohttp middleware. Your code then owns permissions, identity injection,
and namespace/session isolation. See [request context](dependencies.md#what-middleware-decided).

`Registry(auth=...)` leaves console assets public. For application middleware,
wrap its authentication checks to keep the console open:

<!-- name: test_public_console_middleware -->
```python
from aiohttp import web

from aiohttp_tiny_mcp.console import Console


def allow_console(authenticate):
    @web.middleware
    async def middleware(request, handler):
        if Console.is_public(request):
            return await handler(request)
        return await authenticate(request, handler)

    return middleware
```

Install `allow_console(your_auth_middleware)` in `web.Application(middlewares=[...])`.
The exemption covers mounted console assets for GET and HEAD, including prefixed
routes. MCP requests still pass through your authentication checks.
