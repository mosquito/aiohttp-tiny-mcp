"""List MCP tools after browser login with an optional custom success page."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from aiohttp_tiny_mcp import AdapterSet, Client, LoopbackCallback, OAuth


async def main(options: argparse.Namespace) -> None:
    callback_options = (
        {"success_html": options.success_html.read_text()} if options.success_html else {}
    )
    async with LoopbackCallback(port=options.port, **callback_options) as callback:
        async with OAuth(
            options.endpoint,
            issuer=options.issuer,
            client_id=options.client_id,
            metadata_url=options.metadata_url,
            registration={} if options.register else None,
            redirect_uri=callback.redirect_uri,
            allow_http=options.allow_http,
        ) as auth:
            await callback.login(auth)
            adapter = AdapterSet.default().by_version[options.revision]
            async with Client(options.endpoint, adapter, auth=auth) as client:
                await client.initialize()
                for tool in await client.list_tools():
                    print(tool.name)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("endpoint", help="MCP endpoint URL")
    parser.add_argument("--issuer", help="Expected authorization server issuer")
    parser.add_argument("--client-id", help="Pre-registered client ID; requires --issuer")
    parser.add_argument("--metadata-url", help="Hosted HTTPS client metadata document")
    parser.add_argument("--register", action="store_true", help="Allow dynamic client registration")
    parser.add_argument(
        "--port", type=int, default=0, help="Local callback port (default: OS-assigned port)"
    )
    parser.add_argument("--success-html", type=Path, help="HTML file for the successful login page")
    parser.add_argument(
        "--allow-http", action="store_true", help="Allow loopback HTTP for development"
    )
    parser.add_argument("--revision", default="2026-07-28", help="MCP protocol revision")
    asyncio.run(main(parser.parse_args()))
