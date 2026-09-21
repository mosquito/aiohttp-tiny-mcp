# Authentication

Pass an `Authentication` subclass to `Registry(auth=...)`. The policy receives
the HTTP request and returns a verified `Principal`. Both `Endpoint` and
`SseEndpoint` use the same policy. The endpoints check expiry and required
scopes, inject the principal into handlers, and enforce session ownership.

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

Use Authorization when this MCP endpoint is an OAuth protected resource. This
library verifies bearer tokens; it does not implement an authorization server,
token endpoint, or client registration. Your application supplies the token
verification step, whether that means JWT validation or introspection.

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
