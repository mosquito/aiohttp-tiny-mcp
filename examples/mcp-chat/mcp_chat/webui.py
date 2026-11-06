"""Serve the browser application; all chat operations use MCP."""

from pathlib import Path

from aiohttp import web

STATIC = Path(__file__).with_name("static")
ASSETS = frozenset({"app.js", "mcp.js", "app.css", "agent-config.js", "observe.js", "observe.css"})
HEADERS = {
    "Cache-Control": "no-cache",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}


async def asset(request: web.Request) -> web.FileResponse:
    name = request.match_info["name"]
    if name not in ASSETS:
        raise web.HTTPNotFound
    return web.FileResponse(STATIC / name, headers=HEADERS)


async def redirect(request: web.Request) -> web.StreamResponse:
    raise web.HTTPFound(location=request.rel_url.with_path("/ui/", keep_query=True))


def setup(app: web.Application, *, auth_required: bool = False) -> None:
    async def index(request: web.Request) -> web.Response:
        name = "observe.html" if request.path.rstrip("/") == "/observe" else "index.html"
        html = (STATIC / name).read_text()
        html = html.replace("{{AUTH_REQUIRED}}", "true" if auth_required else "false")
        return web.Response(text=html, content_type="text/html", headers=HEADERS)

    app.router.add_get("/", redirect)
    app.router.add_get("/ui", redirect)
    app.router.add_get("/ui/", index)
    app.router.add_get("/observe", index)
    app.router.add_get("/observe/", index)
    app.router.add_get("/ui/static/{name}", asset)
