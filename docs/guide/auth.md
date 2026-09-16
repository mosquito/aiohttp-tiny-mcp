# Authentication

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

Annotate a handler parameter with Principal to receive the verified identity.
It is injected by the server and never appears in the tool input schema.
scopes= on a tool adds a requirement for that tool; required_scopes= on
Authorization applies to every request. Its built-in tool scope check uses the
scopes on Principal before the handler is called.

Invalid tokens return 401; missing required scopes return 403. By default, the
verified principal also becomes the namespace for sessions, subscriptions, and
pending questions. This prevents callers from sharing state. Set
namespace_from_token=False only when your application sets a namespace in its
own trusted middleware.

bind_sessions=True is also the default: a legacy HTTP session belongs to the
principal that opened it. Do not disable it unless a separate trusted layer
binds session IDs to callers.

## Where the metadata has to be served

Endpoint.routes includes the metadata route along with the endpoint, and
Endpoint.metadata_routes returns it alone. The path comes from resource, not
from where you mounted anything, and RFC 8615 places a well-known URI directly
under the authority. Give resource the endpoint's public URL, so the two agree.

<!-- name: async test_auth -->
```python
assert auth.metadata_path == "/.well-known/oauth-protected-resource/reports/mcp"
assert auth.metadata_url == "https://mcp.example.com" + auth.metadata_path
```

A prefix in the path you pass to routes or setup is enough, and keeps the
metadata at the root. Mounting the endpoint inside add_subapp does not: aiohttp
prefixes every route a subapplication holds, including this one, and a client
never looks there. Pass metadata=False to routes and give metadata_routes to
the application that owns the root. See
[under a subapplication](../deployment/transports.md#under-a-subapplication).

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
