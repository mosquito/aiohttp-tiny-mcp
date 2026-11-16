"""Optional HTML console for an MCP endpoint."""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from html import escape
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from aiohttp import web
from pydantic import BaseModel

from aiohttp_tiny_mcp.protocol.schema import inline_refs

log = logging.getLogger("aiohttp_tiny_mcp")

HERE = Path(__file__).parent

FILES = {
    "": ("index.html", "text/html"),
    "console.js": ("console.js", "application/javascript"),
    "console.css": ("console.css", "text/css"),
    "oauth-callback": ("oauth-callback.html", "text/html"),
}


def check_query_property(property_schema: dict[str, Any], name: str, *, item: bool = False) -> None:
    """Require one scalar type or a list of scalars, with optional nullability."""
    branches = property_schema.get("anyOf", property_schema.get("oneOf"))
    if branches is not None:
        candidates = [branch for branch in branches if branch.get("type") != "null"]
        if len(candidates) != 1:
            raise ValueError(f"Query parameter {name!r} must have one non-null type.")
        check_query_property(candidates[0], name, item=item)
        return
    kind = property_schema.get("type")
    if isinstance(kind, list):
        kinds = set(kind) - {"null"}
        if len(kinds) > 1:
            raise ValueError(f"Query parameter {name!r} must have one non-null type.")
        kind = next(iter(kinds), "null")
    if kind == "array" and not item:
        items = property_schema.get("items", {})
        check_query_property(items, name, item=True)
        return
    if kind not in {"string", "integer", "number", "boolean", "null"}:
        raise ValueError(f"Query parameter {name!r} must be a scalar or a list of scalars.")


@dataclass(frozen=True)
class ConsoleEndpoint:
    """Describe an existing MCP route and its query fields for the console."""

    path: str
    description: str
    query: type[BaseModel] | None = None
    auth: Sequence[Literal["none", "basic", "bearer", "query", "custom", "oauth"]] | None = None

    def definition(self) -> dict[str, Any]:
        """Return validated, JSON-compatible endpoint metadata."""
        if (
            not isinstance(self.path, str)
            or not self.path.startswith("/")
            or self.path.startswith("//")
            or "\\" in self.path
            or "#" in self.path
            or any(ord(character) < 32 or ord(character) == 127 for character in self.path)
        ):
            raise ValueError("A console endpoint needs a root-relative path without a fragment.")
        address = urlsplit(self.path)
        if address.scheme or address.netloc:
            raise ValueError("A console endpoint must be on the same origin.")
        if not isinstance(self.description, str) or not self.description.strip():
            raise ValueError("A console endpoint needs a nonempty description.")
        auth = list(self.auth) if self.auth is not None else None
        if auth is not None:
            supported = {"none", "basic", "bearer", "query", "custom", "oauth"}
            if not auth or any(
                not isinstance(method, str) or method not in supported for method in auth
            ):
                raise ValueError(
                    "Console endpoint auth must contain supported authentication types."
                )
            if len(auth) != len(set(auth)):
                raise ValueError("Console endpoint authentication types must be unique.")
        schema: dict[str, Any] = {"type": "object", "properties": {}}
        if self.query is not None:
            if not isinstance(self.query, type) or not issubclass(self.query, BaseModel):
                raise ValueError("A console endpoint query must be a Pydantic model class.")
            try:
                schema = inline_refs(self.query.model_json_schema())
            except RecursionError as error:
                raise ValueError("Recursive console query models are not supported.") from error
            if schema.get("type") != "object" or "properties" not in schema:
                raise ValueError("A console endpoint query must describe named fields.")
            for name, property_schema in schema["properties"].items():
                check_query_property(property_schema, name)
        return {"path": self.path, "description": self.description, "query": schema, "auth": auth}


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
        oauth_client_id: str | None = None,
        endpoints: Sequence[ConsoleEndpoint] | None = None,
    ) -> None:
        self.endpoints = tuple(endpoints or ())
        self.endpoint_definitions = [endpoint.definition() for endpoint in self.endpoints]
        paths = [endpoint.path for endpoint in self.endpoints]
        if len(paths) != len(set(paths)):
            raise ValueError("Console endpoint paths must be unique.")
        if not oauth_client_id and any(
            "oauth" in (definition["auth"] or []) for definition in self.endpoint_definitions
        ):
            raise ValueError("Declared OAuth authentication requires oauth_client_id.")
        self.endpoint_path = paths[0] if paths else endpoint_path
        self.title = title
        self.description = description
        self.template = Path(template) if template else HERE / "index.html"
        self.variables = dict(variables or {})
        self.oauth_client_id = oauth_client_id

    def substitutions(self, base: str) -> dict[str, str]:
        return {
            "ENDPOINT": self.endpoint_path,
            "ENDPOINTS": json.dumps(self.endpoint_definitions),
            "BASE": base,
            "TITLE": self.title,
            "DESCRIPTION": self.description,
            "OAUTH_CLIENT_ID": self.oauth_client_id or "",
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
        """The page, its assets, and the OAuth callback page."""
        base = path.rstrip("/")
        log.debug("console at %s, for the endpoint at %s", base, self.endpoint_path)
        return [
            web.get(base, self.handle),
            web.get(f"{base}/", self.handle),
            web.get(f"{base}/{{file}}", self.handle),
        ]

    @staticmethod
    def is_public(request: web.Request) -> bool:
        """Identify a mounted console asset for application authentication middleware.

        Check the resolved handler, method, and file allowlist. No path prefix
        is trusted, and an unmounted console grants no exemptions.
        """
        handler = request.match_info.handler
        console = getattr(handler, "__self__", None)
        return (
            request.method in {"GET", "HEAD"}
            and isinstance(console, Console)
            and handler == console.handle
            and request.match_info.get("file", "") in FILES
        )

    def setup(self, app: web.Application, path: str = "/console") -> web.Application:
        log.debug("adding the console routes to %r", app)
        app.add_routes(self.routes(path))
        return app


__all__ = ["Console", "ConsoleEndpoint"]
