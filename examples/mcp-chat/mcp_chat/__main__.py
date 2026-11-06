"""Run the MCP chat HTTP server."""

from pathlib import Path

import argclass
from aiohttp import web

from .auth import issue_token
from .server import create_app


class Parser(argclass.Parser):
    """MCP chat with organization-scoped channels, subscriptions, and inboxes."""

    host: str = argclass.Argument(default="127.0.0.1", help="Address to listen on.")
    port: int = argclass.Argument(default=8080, help="Port to listen on.")
    database: Path = argclass.Argument(
        default=Path("mcp-chat.sqlite3"),
        help="SQLite file for chat data, MCP sessions, and events.",
    )
    jwt_secret: str | None = argclass.Argument(
        default=None,
        secret=True,
        help="HS256 signing key (at least 32 bytes); prefer MCP_CHAT_JWT_SECRET.",
    )
    issue_token: bool = argclass.Argument(default=False, help="Print a JWT token and exit.")
    org: str = argclass.Argument(default="", help="Organization for --issue-token.")
    user: str = argclass.Argument(default="", help="User for --issue-token.")
    token_ttl: int = argclass.Argument(default=3600, help="Token lifetime in seconds (1–86400).")


def main() -> None:
    parser = Parser(auto_env_var_prefix="MCP_CHAT_")
    parser.parse_args()
    try:
        if parser.issue_token:
            if parser.jwt_secret is None:
                raise ValueError("Set MCP_CHAT_JWT_SECRET before issuing a token.")
            print(issue_token(parser.jwt_secret, parser.org, parser.user, parser.token_ttl))
            return
        app = create_app(parser.database, jwt_secret=parser.jwt_secret)
    except ValueError as error:
        raise SystemExit(str(error)) from None
    web.run_app(app, host=parser.host, port=parser.port, access_log=None)


if __name__ == "__main__":
    main()
