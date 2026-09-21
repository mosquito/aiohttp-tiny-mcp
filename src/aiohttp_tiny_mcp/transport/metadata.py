"""Mount diagnostics for metadata paths that must stay at the site root."""

from __future__ import annotations

import logging

from aiohttp import web
from aiohttp.web_urldispatcher import AbstractRoute, PlainResource, UrlDispatcher

log = logging.getLogger("aiohttp_tiny_mcp")


class WellKnownResource(PlainResource):
    def __init__(self, path: str, *, name: str | None = None) -> None:
        super().__init__(path, name=name)
        self.expected_path = path

    def add_prefix(self, prefix: str) -> None:
        super().add_prefix(prefix)
        log.warning(
            "OAuth metadata belongs at %s, but a subapplication moved it to %s. "
            "Clients cannot discover this metadata at the prefixed path. "
            "Mount metadata_routes() on the root application and use metadata=False "
            "in the subapplication; mount OAuthServer routes on the root application.",
            self.expected_path,
            self.canonical,
        )


class WellKnownRoute(web.RouteDef):
    def register(self, router: UrlDispatcher) -> list[AbstractRoute]:
        kwargs = dict(self.kwargs)
        resource = WellKnownResource(self.path, name=kwargs.pop("name", None))
        router.register_resource(resource)
        if kwargs.pop("allow_head", True):
            resource.add_route("HEAD", self.handler, **kwargs)
        return [resource.add_route(self.method, self.handler, **kwargs)]


def metadata_route(route: web.RouteDef) -> web.RouteDef:
    """Keep GET metadata routes compatible with add_routes and warn on prefixing."""
    if route.method != "GET" or not route.path.startswith("/.well-known/"):
        return route
    return WellKnownRoute(route.method, route.path, route.handler, route.kwargs)
