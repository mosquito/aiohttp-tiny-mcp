"""Optional browser and loopback callback helper for native applications."""

from __future__ import annotations

import asyncio
import webbrowser
from collections.abc import Awaitable, Callable, Sequence

from aiohttp import web
from yarl import URL

from .callback import SUCCESS_HTML, OAuthCallback
from .oauth import OAuth
from .oauth_helpers import OAuthError


class LoopbackCallback:
    """Run the aiohttp callback on 127.0.0.1 and optionally open a browser.

    Enter this context before constructing OAuth, then call ``login(auth)``.
    Register ``redirect_uri`` with the authorization server, or use DCR. A fixed
    port can be supplied for pre-registered clients. By default, the operating
    system selects a free port. ``success_html`` is trusted
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
        self.port = port
        self.timeout = timeout
        self.open_browser = open_browser
        self.success_html = success_html
        self.runner: web.AppRunner | None = None
        self.callback_uri: str | None = None
        self.callback: OAuthCallback | None = None

    @property
    def redirect_uri(self) -> str:
        if self.callback_uri is None:
            raise RuntimeError("Enter the LoopbackCallback context first.")
        return self.callback_uri

    async def __aenter__(self) -> LoopbackCallback:
        if self.runner is not None:
            raise RuntimeError("The callback listener is already open.")
        app = web.Application()
        app.router.add_get("/callback", self.handle)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        try:
            await web.TCPSite(runner, "127.0.0.1", self.port).start()
        except BaseException:
            await runner.cleanup()
            raise
        self.runner = runner
        port = runner.addresses[0][1]
        self.callback_uri = f"http://127.0.0.1:{port}/callback"
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self.callback is not None:
            self.callback.cancel()
        if self.runner is not None:
            await self.runner.cleanup()
            self.runner = None
        self.callback_uri = None

    async def login(self, auth: OAuth, *, scopes: Sequence[str] | None = None) -> None:
        """Open the browser and wait until the token exchange and save finish."""
        if auth.redirect_uri != self.redirect_uri:
            raise ValueError("OAuth uses another callback URI.")
        if self.callback is not None:
            raise RuntimeError("A browser authorization is already in progress.")
        callback = OAuthCallback(auth, success_html=self.success_html)
        self.callback = callback
        try:
            attempt = await callback.begin(scopes=scopes)

            async def interact() -> None:
                if self.open_browser is not None:
                    await self.open_browser(attempt.url)
                elif not await asyncio.to_thread(webbrowser.open, attempt.url):
                    raise OAuthError("The browser could not be opened.")
                await callback.wait()

            await asyncio.wait_for(interact(), timeout=self.timeout)
        finally:
            callback.cancel()
            self.callback = None

    async def handle(self, request: web.Request) -> web.Response:
        if request.host != URL(self.redirect_uri).authority:
            return OAuthCallback.response(False, "Invalid callback host.")
        if self.callback is None:
            return OAuthCallback.response(False, "No matching authorization attempt.")
        return await self.callback.handle(request)


__all__ = ["LoopbackCallback"]
