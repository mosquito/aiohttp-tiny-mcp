#!/usr/bin/env -S uv run --with pillow --with imagequant==1.1.5 --with pyoxipng==9.1.1 python
"""Render the bundled PNGs from a pinned Material Symbols revision."""

from __future__ import annotations

import asyncio
import json
from base64 import b64decode
from html import escape
from io import BytesIO
from pathlib import Path
from urllib.request import urlopen
from xml.etree import ElementTree

import imagequant
import oxipng
from PIL import Image, PngImagePlugin
from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[1]
REVISION = "10ecc14790612ca8fd164e3f4ffe9c16fc50d5b7"
SOURCE = f"https://raw.githubusercontent.com/google/material-design-icons/{REVISION}"
SVG = "{http://www.w3.org/2000/svg}"
SIZE = 128
RENDER_SIZE = 1024
SYMBOLS = {
    "magnifying-glass": "search",
    "plus": "add",
    "pen": "edit",
    "trash-can": "delete",
    "floppy-disk": "save",
    "file": "draft",
    "folder": "folder",
    "database": "database",
    "gear": "settings",
    "user": "person",
    "users": "group",
    "lock": "lock",
    "key": "key",
    "link": "link",
    "upload": "upload",
    "download": "download",
    "arrows-rotate": "sync",
    "play": "play_arrow",
    "stop": "stop",
    "circle-info": "info",
    "triangle-exclamation": "warning",
    "check": "check",
    "xmark": "close",
    "code": "code",
    "box": "inventory_2",
    "layer-group": "layers",
    "network-wired": "lan",
    "hard-drive": "hard_drive",
    "terminal": "terminal",
    "file-lines": "description",
    "chart-line": "show_chart",
    "hourglass-half": "hourglass_bottom",
    "broom": "cleaning_services",
    "comments": "forum",
    "hashtag": "tag",
    "paper-plane": "send",
    "inbox": "inbox",
    "clock-rotate-left": "history",
    "bell": "notifications",
    "bell-slash": "notifications_off",
    "reply": "reply",
}


def download(path: str) -> bytes:
    with urlopen(f"{SOURCE}/{path}", timeout=30) as response:
        return response.read()


def png(alpha: Image.Image, *, dark: bool, attribution: str) -> bytes:
    image = Image.new("RGBA", alpha.size, "white" if dark else "black")
    image.putalpha(alpha)
    image = imagequant.quantize_pil_image(
        image, dithering_level=0, max_colors=8, min_quality=0, max_quality=100
    )
    opacity = bytes(image.getpalette("RGBA")[3:32:4])
    image.putpalette([255 if dark else 0] * 24)
    metadata = PngImagePlugin.PngInfo()
    metadata.add_text("Comment", attribution, zip=True)
    output = BytesIO()
    image.save(output, format="PNG", bits=4, transparency=opacity, pnginfo=metadata)
    return oxipng.optimize_from_memory(output.getvalue(), level=4)


def source_svg(path: str) -> str:
    tree = ElementTree.fromstring(download(path))
    if tree.tag != SVG + "svg" or not len(tree):
        raise ValueError(f"{path}: expected an SVG with paths")
    if any(node.tag != SVG + "path" or set(node.attrib) != {"d"} or len(node) for node in tree):
        raise ValueError(f"{path}: expected paths without other attributes")
    view_box = escape(tree.attrib["viewBox"], quote=True)
    paths = "".join(f'<path d="{escape(node.attrib["d"], quote=True)}"/>' for node in tree)
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{view_box}" '
        f'width="{RENDER_SIZE}" height="{RENDER_SIZE}">{paths}</svg>'
    )


async def main() -> None:
    target = ROOT / "src/aiohttp_tiny_mcp/data/icons"
    target.mkdir(parents=True, exist_ok=True)
    icons = {}
    async with async_playwright() as engine:
        browser = await engine.chromium.launch()
        page = await browser.new_page()
        for slug, symbol in SYMBOLS.items():
            path = f"symbols/web/{symbol}/materialsymbolsrounded/{symbol}_fill1_24px.svg"
            svg = await asyncio.to_thread(source_svg, path)
            rendered = await page.evaluate(
                """async ([svg, size]) => {
                    const image = new Image();
                    image.src = 'data:image/svg+xml,' + encodeURIComponent(svg);
                    await image.decode();
                    const canvas = document.createElement('canvas');
                    canvas.width = canvas.height = size;
                    canvas.getContext('2d').drawImage(image, 0, 0, size, size);
                    return canvas.toDataURL().split(",")[1];
                }""",
                [svg, RENDER_SIZE],
            )
            with Image.open(BytesIO(b64decode(rendered))) as image:
                alpha = image.getchannel("A").resize((SIZE, SIZE), Image.Resampling.LANCZOS)
            attribution = (
                "Google Material Symbols (Apache-2.0).\n"
                "Modified: rasterized, resized and quantized for light and dark themes."
            )
            for dark, suffix in ((False, ""), (True, "-dark")):
                (target / f"{slug}{suffix}.png").write_bytes(
                    png(alpha, dark=dark, attribution=attribution)
                )
            icons[slug.upper().replace("-", "_")] = path
        await browser.close()
    manifest = {
        "project": "Google Material Symbols",
        "revision": REVISION,
        "source": SOURCE,
        "style": "Rounded Fill",
        "license": "Apache-2.0",
        "changes": "Rasterized, resized and quantized for light and dark themes.",
        "icons": icons,
    }
    (target / "SOURCE.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    asyncio.run(main())
