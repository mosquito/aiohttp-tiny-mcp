"""Optional browser and loopback callback helper for native applications."""

from __future__ import annotations

import asyncio
import webbrowser
from collections.abc import Awaitable, Callable, Sequence

from aiohttp import web
from yarl import URL

from ._oauth import OAuthError
from .callback import SUCCESS_HTML, OAuthCallback
from .oauth import OAuth


class LoopbackCallback:
    """Run the aiohttp callback on 127.0.0.1 and optionally open a browser.

    Enter this context before constructing OAuth, then call ``login(auth)``.
    Register ``redirect_uri`` with the authorization server, or use DCR. A fixed
    port can be supplied for pre-registered clients. ``success_html`` is trusted
    application HTML, served unchanged only after the token exchange and save.
    """

    def __init__(
        self,
        *,
        port: int = 0,
        timeout: float = 300,
        open_browser: Callable[[str], Awaitable[None]] | None = None,
        success_html: str = SUCCESS_HTML,
    ) -> None:
        if timeout <= 0:
            raise ValueError("The callback timeout must be positive.")
        self._port = port
        self._timeout = timeout
        self._open_browser = open_browser
        self._success_html = success_html
        self._runner: web.AppRunner | None = None
        self._redirect_uri: str | None = None
        self._callback: OAuthCallback | None = None

    @property
    def redirect_uri(self) -> str:
        if self._redirect_uri is None:
            raise RuntimeError("Enter the LoopbackCallback context first.")
        return self._redirect_uri

    async def __aenter__(self) -> LoopbackCallback:
        if self._runner is not None:
            raise RuntimeError("The callback listener is already open.")
        app = web.Application()
        app.router.add_get("/callback", self._handle)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        try:
            await web.TCPSite(runner, "127.0.0.1", self._port).start()
        except BaseException:
            await runner.cleanup()
            raise
        self._runner = runner
        port = runner.addresses[0][1]
        self._redirect_uri = f"http://127.0.0.1:{port}/callback"
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._callback is not None:
            self._callback.cancel()
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
        self._redirect_uri = None

    async def login(self, auth: OAuth, *, scopes: Sequence[str] | None = None) -> None:
        """Open the browser and wait until the token exchange and save finish."""
        if auth.redirect_uri != self.redirect_uri:
            raise ValueError("OAuth uses another callback URI.")
        if self._callback is not None:
            raise RuntimeError("A browser authorization is already in progress.")
        callback = OAuthCallback(auth, success_html=self._success_html)
        self._callback = callback
        try:
            attempt = await callback.begin(scopes=scopes)

            async def interact() -> None:
                if self._open_browser is not None:
                    await self._open_browser(attempt.url)
                elif not await asyncio.to_thread(webbrowser.open, attempt.url):
                    raise OAuthError("The browser could not be opened.")
                await callback.wait()

            await asyncio.wait_for(interact(), timeout=self._timeout)
        finally:
            callback.cancel()
            self._callback = None

    async def _handle(self, request: web.Request) -> web.Response:
        if request.host != URL(self.redirect_uri).authority:
            return OAuthCallback._response(False, "Invalid callback host.")
        if self._callback is None:
            return OAuthCallback._response(False, "No matching authorization attempt.")
        return await self._callback.handle(request)


__all__ = ["LoopbackCallback"]
