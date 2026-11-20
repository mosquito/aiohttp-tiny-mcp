import shutil
import subprocess
from collections.abc import AsyncIterator, Callable
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

from aiohttp_tiny_mcp import Endpoint, Registry
from aiohttp_tiny_mcp.console import Console


@pytest.fixture
async def served(registry: Registry) -> AsyncIterator[TestClient]:
    app = Endpoint(registry).app("/mcp")
    Console("/mcp").setup(app, "/console")
    async with TestClient(TestServer(app)) as client:
        yield client


@pytest.fixture
def run_js() -> Callable[[str], None]:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    script = Path(__file__).resolve().parents[2] / "src/aiohttp_tiny_mcp/console/console.js"

    def run(body: str) -> None:
        source = script.read_text()
        source = source[: source.index("const page =")]
        driver = (
            'const assert = require("node:assert/strict");\n'
            + source
            + "\n(async () => {\n"
            + body
            + "\n})().catch(error => { console.error(error); process.exit(1); });"
        )
        result = subprocess.run([node, "-e", driver], capture_output=True, text=True, timeout=10)
        assert result.returncode == 0, result.stderr

    return run
