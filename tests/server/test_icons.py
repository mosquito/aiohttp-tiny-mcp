from __future__ import annotations

import json
import struct
import zlib
from base64 import b64decode

import pytest
from pydantic import BaseModel

from aiohttp_tiny_mcp import Icon, Icons, Registry
from aiohttp_tiny_mcp.icons import ICON_FILES
from aiohttp_tiny_mcp.protocol.selection import AdapterSet
from aiohttp_tiny_mcp.testing import over_http


def chunks(data):
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    offset = 8
    found = {}
    while offset < len(data):
        size = struct.unpack_from("!I", data, offset)[0]
        kind = data[offset + 4 : offset + 8]
        payload = data[offset + 8 : offset + 8 + size]
        assert struct.unpack_from("!I", data, offset + 8 + size)[0] == zlib.crc32(kind + payload)
        found[kind] = found.get(kind, b"") + payload
        offset += 12 + size
    return found


@pytest.mark.parametrize("preset", list(Icons), ids=lambda icon: icon.name)
def test_presets_are_embedded_indexed_png_pairs(preset):
    light, dark = preset.value
    for theme, icon in (("light", light), ("dark", dark)):
        assert icon.theme == theme
        assert icon.mime_type == "image/png"
        assert icon.sizes == ["128x128"]
        data = b64decode(icon.src.removeprefix("data:image/png;base64,"))
        parts = chunks(data)
        width, height, depth, color, compression, filtering, interlace = struct.unpack(
            "!IIBBBBB", parts[b"IHDR"]
        )
        assert (width, height, color, compression, filtering, interlace) == (128, 128, 3, 0, 0, 0)
        assert depth in (4, 8)
        palette = parts[b"PLTE"]
        assert 2 * 3 <= len(palette) <= 8 * 3
        assert palette == bytes([0 if theme == "light" else 255]) * len(palette)
        opacity = sorted(parts[b"tRNS"].ljust(len(palette) // 3, b"\xff"))
        assert len(set(opacity)) >= 2
        assert opacity[0] < 32 and opacity[-1] > 223
        keyword, compressed = parts[b"zTXt"].split(b"\x00", 1)
        assert keyword == b"Comment"
        assert compressed[0] == 0
        attribution = zlib.decompress(compressed[1:])
        assert b"Google Material Symbols" in attribution
        assert b"Apache-2.0" in attribution
        assert b"Modified:" in attribution


def test_the_catalogue_matches_its_pinned_source_manifest():
    source = json.loads((ICON_FILES / "SOURCE.json").read_text())
    assert len(Icons) == 41
    assert set(source["icons"]) == {icon.name for icon in Icons}
    assert (
        source["icons"]["MAGNIFYING_GLASS"]
        == "symbols/web/search/materialsymbolsrounded/search_fill1_24px.svg"
    )


def test_registered_presets_have_independent_metadata():
    first = Registry("first", "1", icons=[Icons.CODE])
    second = Registry("second", "1", icons=[Icons.CODE])
    first.info.icons[0].sizes.append("1x1")
    assert second.info.icons[0].sizes == ["128x128"]
    assert Icons.CODE.value[0].sizes == ["128x128"]


def test_from_file_delegates_to_from_bytes(tmp_path, monkeypatch):
    image = tmp_path / "custom.png"
    image.write_bytes((ICON_FILES / "magnifying-glass.png").read_bytes())
    called = []
    original = Icon.from_bytes

    def from_bytes(cls, data, **options):
        called.append((data, options))
        return original(data, **options)

    monkeypatch.setattr(Icon, "from_bytes", classmethod(from_bytes))
    icon = Icon.from_file(image, sizes=["128x128"], theme="light")
    assert called == [
        (image.read_bytes(), {"mime_type": "image/png", "sizes": ["128x128"], "theme": "light"})
    ]
    assert b64decode(icon.src.split(",", 1)[1]) == image.read_bytes()
    light, dark = Icon.from_files(image, image)
    assert light.theme == "light" and dark.theme == "dark"


def test_from_bytes_supports_custom_images_and_from_file_reports_missing_files(tmp_path):
    svg = b'<svg xmlns="http://www.w3.org/2000/svg"/>'
    icon = Icon.from_bytes(svg, mime_type="image/svg+xml", sizes=["any"])
    assert icon.mime_type == "image/svg+xml"
    assert b64decode(icon.src.split(",", 1)[1]) == svg
    with pytest.raises(FileNotFoundError):
        Icon.from_file(tmp_path / "missing.png")


class Nothing(BaseModel):
    pass


class Entry(BaseModel):
    id: str


@pytest.mark.parametrize("version", AdapterSet.default().versions)
async def test_presets_and_custom_icons_work_on_every_declaration(version):
    custom = Icon(src="https://example.test/custom.png")
    registry = Registry("icons", "1.0", icons=[Icons.CODE, custom])
    icons = [Icons.MAGNIFYING_GLASS, custom]

    @registry.tool(icons=icons)
    async def status(args: Nothing) -> str:
        return "ready"

    registry.resource("status://service", status, icons=icons)
    registry.prompt(status, icons=icons)

    @registry.resource("entry://{id}", icons=icons)
    async def entry(args: Entry) -> str:
        return args.id

    async with over_http(registry, adapter=version) as client:
        info = await client.initialize()
        definitions = [
            (await client.list_tools())[0],
            (await client.list_resources())[0],
            (await client.list_resource_templates())[0],
            (await client.list_prompts())[0],
        ]
    expected = version in ("2026-07-28", "2025-11-25")
    for definition in definitions:
        if expected:
            assert [icon.theme for icon in definition.icons] == ["light", "dark", None]
            assert definition.icons[-1] == custom
            assert definition.icons[0].src.startswith("data:image/png;base64,")
        else:
            assert definition.icons is None
    server_info = (
        info.get("serverInfo") or (info.get("_meta") or {})["io.modelcontextprotocol/serverInfo"]
    )
    assert bool(server_info.get("icons")) is expected
