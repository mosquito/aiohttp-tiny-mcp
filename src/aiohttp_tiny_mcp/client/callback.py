"""OAuth callbacks for an existing aiohttp application."""

from __future__ import annotations

import asyncio
import hmac
from collections.abc import Sequence

from aiohttp import web
from yarl import URL

from ._oauth import OAuthError
from .oauth import AuthorizationAttempt, OAuth

SUCCESS_HTML = """<!doctype html>
<html lang="en"><meta charset="utf-8"><title>Authorization complete</title>
<h1>Authorization complete</h1><p>You can close this window.</p></html>"""


class OAuthCallback:
    """Mount a local or remote callback for one OAuth account context.

    The application must authenticate the user before calling ``begin`` and
    keep each user's context separate. The external HTTPS ``auth.redirect_uri``
    can differ from the listener address behind a trusted reverse proxy.
    """

    def __init__(self, auth: OAuth, *, success_html: str = SUCCESS_HTML) -> None:
        self.auth = auth
        self.success_html = success_html
        self._attempt: AuthorizationAttempt | None = None
        self._outcome: asyncio.Future[Exception | None] | None = None

    def routes(self) -> list[web.RouteDef]:
        """Return the GET route at the path of the configured redirect URI."""
        return [web.get(URL(self.auth.redirect_uri).path, self.handle)]

    def setup(self, app: web.Application) -> None:
        """Mount the callback on an existing application."""
        app.add_routes(self.routes())

    async def begin(self, *, scopes: Sequence[str] | None = None) -> AuthorizationAttempt:
        """Prepare the URL to which the application redirects its signed-in user."""
        self.cancel()
        self._attempt = await self.auth.begin(scopes=scopes)
        self._outcome = asyncio.get_running_loop().create_future()
        return self._attempt

    async def wait(self) -> None:
        """Wait for token acquisition and persistence, or raise their error."""
        if self._outcome is None:
            raise RuntimeError("Start an authorization attempt first.")
        error = await asyncio.shield(self._outcome)
        if error is not None:
            raise error

    def cancel(self) -> None:
        """Cancel a pending callback without changing existing credentials."""
        if self._attempt is not None:
            self.auth.cancel(self._attempt)
            self._attempt = None
        if self._outcome is not None and not self._outcome.done():
            self._outcome.set_result(OAuthError("Authorization was cancelled."))

    async def handle(self, request: web.Request) -> web.Response:
        """Exchange the callback code before sending the result page."""
        attempt, outcome = self._attempt, self._outcome
        states = request.query.getall("state", [])
        expected_state = URL(attempt.url).query["state"] if attempt is not None else ""
        if (
            attempt is None
            or outcome is None
            or outcome.done()
            or len(states) != 1
            or not hmac.compare_digest(states[0].encode(), expected_state.encode())
        ):
            return self._response(False, "No matching authorization attempt.")
        self._attempt = None
        # Use the configured public URI, not an untrusted Host or Forwarded header.
        callback_url = self.auth.redirect_uri + "?" + request.rel_url.raw_query_string
        try:
            await self.auth.complete(attempt, callback_url)
        except asyncio.CancelledError:
            if not outcome.done():
                outcome.set_result(OAuthError("The callback request was cancelled."))
            raise
        except Exception as error:
            if not outcome.done():
                outcome.set_result(error)
            return self._response(False, "Authorization failed. Return to the application.")
        if outcome.done():
            return self._response(False, "Authorization was cancelled.")
        outcome.set_result(None)
        return self._response(True, self.success_html)

    @staticmethod
    def _response(success: bool, text: str) -> web.Response:
        return web.Response(
            status=200 if success else 400,
            text=text,
            content_type="text/html" if success else "text/plain",
            headers={
                "Cache-Control": "no-store",
                "Referrer-Policy": "no-referrer",
                "Content-Security-Policy": (
                    "default-src 'none'; style-src 'unsafe-inline'; "
                    "img-src data: https:; frame-ancestors 'none'"
                ),
            },
        )


__all__ = ["OAuthCallback"]
