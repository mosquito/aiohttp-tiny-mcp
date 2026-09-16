"""Optional HTML console for an MCP endpoint."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from html import escape
from pathlib import Path

from aiohttp import web

log = logging.getLogger("aiohttp_tiny_mcp")

HERE = Path(__file__).parent

FILES = {
    "": ("index.html", "text/html"),
    "console.js": ("console.js", "application/javascript"),
    "console.css": ("console.css", "text/css"),
}


class Console:
    """Serve the console."""

    def __init__(
        self,
        endpoint_path: str = "/mcp",
        *,
        title: str = "MCP console",
        description: str = "",
        template: str | Path | None = None,
        variables: Mapping[str, str] | None = None,
    ) -> None:
        self.endpoint_path = endpoint_path
        self.title = title
        self.description = description
        self.template = Path(template) if template else HERE / "index.html"
        self.variables = dict(variables or {})

    def substitutions(self, base: str) -> dict[str, str]:
        return {
            "ENDPOINT": self.endpoint_path,
            "BASE": base,
            "TITLE": self.title,
            "DESCRIPTION": self.description,
            **self.variables,
        }

    def render(self, name: str, base: str = "/console") -> str:
        source = self.template if name == "index.html" else HERE / name
        text = source.read_text(encoding="utf-8")
        for key, value in self.substitutions(base).items():
            text = text.replace("{{" + key + "}}", escape(str(value), quote=True))
        return text

    def reached(self, request: web.Request, wanted: str) -> str:
        """The prefix the browser used, taken from this request.

        The page prints absolute addresses, and only the request knows them. A
        subapplication adds its prefix to the request path and not to the route,
        so a stored prefix would be wrong there.
        """
        path = request.path
        if wanted:
            path = path[: -(len(wanted) + 1)]
        return path.rstrip("/")

    async def handle(self, request: web.Request) -> web.StreamResponse:
        wanted = request.match_info.get("file", "")
        found = FILES.get(wanted)
        if found is None:
            raise web.HTTPNotFound
        name, content_type = found
        return web.Response(
            text=self.render(name, self.reached(request, wanted)),
            content_type=content_type,
            charset="utf-8",
            headers={"Cache-Control": "no-store"},
        )

    def routes(self, path: str = "/console") -> list[web.RouteDef]:
        """The page, either way it is addressed, and its two files."""
        base = path.rstrip("/")
        log.debug("console at %s, for the endpoint at %s", base, self.endpoint_path)
        return [
            web.get(base, self.handle),
            web.get(f"{base}/", self.handle),
            web.get(f"{base}/{{file}}", self.handle),
        ]

    def setup(self, app: web.Application, path: str = "/console") -> web.Application:
        log.debug("adding the console routes to %r", app)
        app.add_routes(self.routes(path))
        return app


__all__ = ["Console"]
