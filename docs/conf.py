"""Sphinx configuration. Prose lives in Markdown, parsed by MyST."""

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
html_theme_options = {
    "source_repository": "https://github.com/mosquito/aiohttp-tiny-mcp",
    "source_branch": "master",
    "source_directory": "docs/",
}

autodoc_member_order = "bysource"
autodoc_typehints = "description"

exclude_patterns = ["_build"]
