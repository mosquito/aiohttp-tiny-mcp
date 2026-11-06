from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

from mcp_chat.server import create_app


@pytest.mark.parametrize("protected", [False, True])
@pytest.mark.parametrize("path", ["/ui/", "/observe"])
async def test_ui_reports_auth_mode_without_disclosing_secret(
    protected: bool, path: str, tmp_path: Path
) -> None:
    secret = "webui-test-key-32-bytes-minimum-secret"
    app = create_app(tmp_path / "chat.sqlite3", jwt_secret=secret if protected else None)
    async with TestClient(TestServer(app)) as client:
        response = await client.get(path)
        html = await response.text()
        assert f'data-auth-required="{str(protected).lower()}"' in html
        assert "{{AUTH_REQUIRED}}" not in html
        assert secret not in html


@pytest.mark.parametrize("path", ["/", "/ui"])
async def test_ui_redirect_preserves_identity(path: str, tmp_path: Path) -> None:
    async with TestClient(TestServer(create_app(tmp_path / "chat.sqlite3"))) as client:
        response = await client.get(f"{path}?org=example.com&user=alice", allow_redirects=False)
        assert response.status == 302
        assert response.headers["Location"] == "/ui/?org=example.com&user=alice"


@pytest.mark.parametrize(
    ("path", "mime"),
    [
        ("/ui/", "text/html"),
        ("/ui/static/app.css", "text/css"),
        ("/ui/static/app.js", "text/javascript"),
        ("/ui/static/mcp.js", "text/javascript"),
        ("/ui/static/agent-config.js", "text/javascript"),
        ("/observe", "text/html"),
        ("/ui/static/observe.js", "text/javascript"),
        ("/ui/static/observe.css", "text/css"),
    ],
)
async def test_ui_assets_are_public(path: str, mime: str, tmp_path: Path) -> None:
    async with TestClient(TestServer(create_app(tmp_path / "chat.sqlite3"))) as client:
        response = await client.get(path)
        assert response.status == 200
        assert response.content_type in {mime, "application/javascript"}
        assert await response.text()
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        response = await client.head(path)
        assert response.status == 200
        assert await response.read() == b""


@pytest.mark.parametrize(
    "name", ["server.py", "store.py", "missing.js", "index.html", "..%2fserver.py"]
)
async def test_ui_does_not_expose_other_files(name: str, tmp_path: Path) -> None:
    async with TestClient(TestServer(create_app(tmp_path / "chat.sqlite3"))) as client:
        response = await client.get(f"/ui/static/{name}")
        assert response.status == 404
