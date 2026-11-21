from collections.abc import AsyncIterator, Awaitable, Callable

import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import BaseTestServer, TestServer
from playwright.async_api import Browser, BrowserContext, async_playwright
from pydantic import BaseModel

from aiohttp_tiny_mcp import Endpoint, Registry
from aiohttp_tiny_mcp.console import Console, ConsoleEndpoint


class EndpointQuery(BaseModel):
    org: str = "example.com"
    user: str = "human"


class TokenQuery(BaseModel):
    token: str | None = None


class ResourceArguments(BaseModel):
    pass


class ResourceEntry(BaseModel):
    name: str


@pytest.fixture
def console_app() -> Callable[..., web.Application]:
    def create_app(token: str | None = None) -> web.Application:
        app = Endpoint(Registry("test_server", "1")).app()
        if token is not None:

            @web.middleware
            async def authenticate(
                request: web.Request,
                handler: Callable[[web.Request], Awaitable[web.StreamResponse]],
            ) -> web.StreamResponse:
                if request.path == "/mcp" and not (
                    request.headers.get("Authorization") == f"Bearer {token}"
                    or request.query.get("token") == token
                ):
                    raise web.HTTPUnauthorized(headers={"WWW-Authenticate": "Bearer"})
                return await handler(request)

            app.middlewares.append(authenticate)
        Console(
            endpoints=[
                ConsoleEndpoint(
                    "/mcp",
                    "Test endpoint",
                    TokenQuery if token is not None else EndpointQuery,
                    auth=("bearer", "query") if token is not None else ("none",),
                    server_name="test_server",
                )
            ]
        ).setup(app)
        return app

    return create_app


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def browser() -> AsyncIterator[Browser]:
    async with async_playwright() as engine:
        running = await engine.chromium.launch()
        try:
            yield running
        finally:
            await running.close()


@pytest_asyncio.fixture(loop_scope="session")
async def context(browser: Browser) -> AsyncIterator[BrowserContext]:
    running = await browser.new_context(viewport={"width": 1440, "height": 900})
    try:
        yield running
    finally:
        await running.close()


@pytest_asyncio.fixture(loop_scope="session")
async def server(
    console_app: Callable[..., web.Application], context: BrowserContext
) -> AsyncIterator[BaseTestServer]:
    async with TestServer(console_app()) as running:
        try:
            yield running
        finally:
            await context.close()


@pytest_asyncio.fixture(loop_scope="session")
async def resource_server(context: BrowserContext) -> AsyncIterator[BaseTestServer]:
    registry = Registry("resource-console", "1")

    @registry.resource("file://alpha", mime_type="text/plain")
    async def alpha(args: ResourceArguments) -> str:
        return "alpha content"

    @registry.resource("file://entries/{name}")
    async def entry(args: ResourceEntry) -> str:
        return f"entry {args.name}"

    app = Endpoint(registry).app()
    Console().setup(app)
    async with TestServer(app) as running:
        try:
            yield running
        finally:
            await context.close()
