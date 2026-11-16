import json
from html.parser import HTMLParser
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

from mcp_chat.server import create_app


class ConsolePage(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.attributes: dict[str, str | None] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "html":
            self.attributes = dict(attrs)


@pytest.mark.parametrize("authenticated", [False, True], ids=["demo", "jwt"])
async def test_console_declares_query_fields_for_the_authentication_mode(
    tmp_path: Path, authenticated: bool
) -> None:
    secret = "console-test-secret-with-at-least-32-bytes" if authenticated else None
    async with TestClient(
        TestServer(create_app(tmp_path / "chat.sqlite3", jwt_secret=secret))
    ) as client:
        response = await client.get("/console")
        assert response.status == 200
        html = await response.text()
    parser = ConsolePage()
    parser.feed(html)
    metadata = parser.attributes["data-endpoints"]
    assert metadata is not None
    declarations = json.loads(metadata)
    assert len(declarations) == 1
    endpoint = declarations[0]
    assert endpoint["path"] == "/mcp"
    assert endpoint["description"]
    query = endpoint["query"]
    fields = query["properties"]
    if authenticated:
        assert endpoint["auth"] == ["bearer", "query"]
        assert secret is not None
        assert set(fields) == {"token"}
        assert fields["token"]["default"] is None
        assert "token" not in query.get("required", [])
        assert "Bearer" in fields["token"]["description"]
        assert secret not in html
    else:
        assert endpoint["auth"] == ["none"]
        assert set(fields) == {"org", "user"}
        assert fields["org"]["default"] == "example.com"
        assert fields["user"]["default"] == "human"
        assert all(field["description"] for field in fields.values())
