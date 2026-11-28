"""Sphinx configuration. Prose lives in Markdown, parsed by MyST."""

from os.path import relpath
from pathlib import Path

from sphinx.util.docutils import SphinxDirective

project = "aiohttp-tiny-mcp"
language = "en"
author = "Dmitry Orlov"
copyright = "2026, Dmitry Orlov"  # noqa: A001 -- the name Sphinx reads

extensions = [
    "myst_parser",
    "sphinxcontrib.mermaid",
    "sphinx.ext.autodoc",
    "sphinx.ext.napoleon",
    "sphinx.ext.intersphinx",
    "sphinx.ext.viewcode",
]

myst_enable_extensions = ["colon_fence", "deflist", "fieldlist", "substitution"]
myst_heading_anchors = 3

intersphinx_mapping = {
    "python": ("https://docs.python.org/3", None),
    "aiohttp": ("https://docs.aiohttp.org/en/stable", None),
    "pydantic": ("https://docs.pydantic.dev/latest", None),
}

html_theme = "furo"
html_title = "aiohttp-tiny-mcp"
html_static_path = ["_static"]
html_css_files = ["icons.css"]
html_theme_options = {
    "source_repository": "https://github.com/mosquito/aiohttp-tiny-mcp",
    "source_branch": "master",
    "source_directory": "docs/",
}

autodoc_member_order = "bysource"
autodoc_typehints = "description"

exclude_patterns = ["_build"]


class IconCatalogue(SphinxDirective):
    def run(self):
        from aiohttp_tiny_mcp.icons import Icons

        self.env.note_dependency("conf.py")
        self.env.note_dependency("../src/aiohttp_tiny_mcp/icons.py")
        rows = [
            ":::{table} Built-in icon presets",
            ":class: icon-catalogue",
            "",
            "| No. | Preset | Light | Dark |",
            "| ---: | --- | --- | --- |",
        ]
        asset_dir = Path(__file__).resolve().parents[1] / "src/aiohttp_tiny_mcp/data/icons"
        page_dir = Path(self.env.doc2path(self.env.docname)).parent
        for number, preset in enumerate(Icons, start=1):
            slug = preset.name.lower().replace("_", "-")
            path = relpath(asset_dir / slug, page_dir)
            light = f"![{slug} light]({path}.png)"
            dark = f"![{slug} dark]({path}-dark.png)"
            rows.append(f"| {number} | `Icons.{preset.name}` | {light} | {dark} |")
        rows.append(":::")
        return self.parse_text_to_nodes("\n".join(rows))


def setup(app):
    app.add_directive("icon-catalogue", IconCatalogue)
