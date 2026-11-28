# Icons

Pass your own `Icon` descriptors or choose a built-in preset from `Icons`.
The same `icons=` option is available on the registry, tools, fixed resources,
resource templates and prompts.

<!-- name: async test_icons; fixtures: serve -->
```python
from pydantic import BaseModel

from aiohttp_tiny_mcp import Client, Icon, Icons, Registry
from aiohttp_tiny_mcp.protocol.selection import AdapterSet

custom = Icon(src="https://example.com/status.png", sizes=["128x128"])
registry = Registry(
    "catalogue",
    "1.0",
    icons=[Icons.CODE, custom],
    website_url="https://example.com",
)


class Nothing(BaseModel):
    pass


@registry.tool(icons=[Icons.MAGNIFYING_GLASS])
async def search(args: Nothing) -> str:
    """Search the catalogue."""
    return "found"


registry.resource("status://service", search, icons=[Icons.CIRCLE_INFO])
registry.prompt(search, name="review", icons=[Icons.CODE])

url = await serve(registry)
async with Client(url, AdapterSet.default().by_version["2025-11-25"]) as client:
    info = await client.initialize()
    tool = (await client.list_tools())[0]

assert [icon.theme for icon in tool.icons] == ["light", "dark"]
assert tool.icons[0].src.startswith("data:image/png;base64,")
assert info["serverInfo"]["websiteUrl"] == "https://example.com"
```

Each preset expands to two ordinary MCP descriptors at registration. The wire
contains `src`, `mimeType`, `sizes` and `theme`, without enum names. Icons and
`websiteUrl` reach `2025-11-25` and `2026-07-28`; the three older adapters omit them.
Resource tools omit presentation metadata from their model-facing catalogue.

## Built-in presets

Preview every preset in both themes at 48 × 48 CSS pixels.

```{icon-catalogue}
```

Each preset stores two 128×128 PNGs as Base64 data URLs. The light theme uses
black artwork and the dark theme uses white. The generator renders the source
at 1024×1024, downsamples with Lanczos and uses a palette of up to eight entries.
The console displays them at 16 pixels.

The console shows icons beside the server title and catalogue entries. It follows
the browser's colour theme, including theme changes while connected. Custom icons
without a theme are a fallback. Missing, broken or unsupported image sources do
not prevent using the catalogue. To try all declaration sites, run
`uv run python examples/demo.py` and open `http://127.0.0.1:8080/console`.

## Custom embedded images

Use `from_bytes` for image data and `from_file` for a file. The file constructor
infers the MIME type from its extension and calls `from_bytes`. Pass `mime_type=`
to override it. `from_files(light, dark)` calls `from_file` twice and returns a
tuple with both themes. All three constructors accept `sizes=`. The `icons`
argument accepts a `Sequence`, including a list or tuple.

<!-- name: test_icons_files -->
```python
from pathlib import Path
from tempfile import TemporaryDirectory

from aiohttp_tiny_mcp import Icon

svg = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16"/>'
embedded = Icon.from_bytes(svg, mime_type="image/svg+xml", sizes=["any"])

with TemporaryDirectory() as directory:
    path = Path(directory) / "custom.svg"
    path.write_bytes(svg)
    assert Icon.from_file(path, sizes=["any"]) == embedded
    pair = Icon.from_files(path, path, sizes=["any"])

assert [icon.theme for icon in pair] == ["light", "dark"]
```

Pass that pair directly as `icons=pair`, or mix individual descriptors with
presets. A custom `src` may use `http:`, `https:` or `data:`. Image URLs are
requested by the client; the server does not fetch them.

Embedded bytes repeat in every listing that includes the icon. A server identity
icon also repeats in `2026-07-28` response metadata. Use custom URL descriptors
when that payload cost matters.

## Source, licence and regeneration

The artwork comes from [Google Material Symbols Rounded Fill](https://developers.google.com/fonts/docs/material_symbols).
The artwork and Python code are licensed under Apache 2.0. The distribution
includes the license and a notice for the artwork. PNG metadata records the changes,
and `SOURCE.json` records the pinned revision and upstream path for each preset.

Git LFS tracks the PNGs. Run `git lfs pull` in a source checkout to get them.
Wheels and source distributions include the images.

To regenerate the PNG pairs before a release:

```bash
uv run playwright install chromium
./tools/prepare_icons.py
```

The release script downloads the pinned source SVGs and renders both themes.
Loading the package reads the bundled PNGs and creates the data URLs.
