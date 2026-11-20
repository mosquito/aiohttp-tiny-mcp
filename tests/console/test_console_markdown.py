"""Run the console markdown renderer under Node, including HTML-escaping checks. Skip when Node is
unavailable.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

NODE = shutil.which("node")
CONSOLE = (
    Path(__file__).resolve().parents[2] / "src" / "aiohttp_tiny_mcp" / "console" / "console.js"
)

pytestmark = [
    pytest.mark.skipif(NODE is None, reason="node is not installed"),
    pytest.mark.timeout(10),
]


def render(text: str) -> str:
    """Render one description with the page's own function."""
    source = CONSOLE.read_text(encoding="utf-8")
    start = source.index("const SAFE_LINK")
    end = source.index("function prose(", start)
    driver = f"{source[start:end]}\nprocess.stdout.write(markdownHtml({json.dumps(text)}));\n"
    finished = subprocess.run([NODE, "-e", driver], capture_output=True, text=True, check=True)
    return finished.stdout


def test_a_paragraph_is_rewrapped():
    """Prose is wrapped where its author's editor ended, which is not where
    the panel ends."""
    assert render("One line\nwrapped by an editor.") == "<p>One line wrapped by an editor.</p>"


def test_a_blank_line_starts_another_paragraph():
    assert render("First.\n\nSecond.") == "<p>First.</p><p>Second.</p>"


def test_marks_become_marks():
    assert render("**Never** do this, it is *slow*.") == (
        "<p><strong>Never</strong> do this, it is <em>slow</em>.</p>"
    )


def test_a_code_span_is_code():
    assert render("Call `ex.ask` first.") == "<p>Call <code>ex.ask</code> first.</p>"


def test_marks_inside_a_code_span_are_left_alone():
    assert render("`**not bold**`") == "<p><code>**not bold**</code></p>"


def test_a_bullet_list_is_a_list():
    assert render("Do this:\n\n- first\n- second") == (
        "<p>Do this:</p><ul><li>first</li><li>second</li></ul>"
    )


def test_a_numbered_list_is_ordered():
    assert render("1. one\n2. two") == "<ol><li>one</li><li>two</li></ol>"


def test_a_fenced_block_keeps_its_lines():
    assert render("Like so:\n\n```\nawait ex.ask()\nreturn 1\n```") == (
        "<p>Like so:</p><pre><code>await ex.ask()\nreturn 1</code></pre>"
    )


def test_an_indented_block_is_code_too():
    assert render("Like so:\n\n    await ex.ask()") == (
        "<p>Like so:</p><pre><code>await ex.ask()</code></pre>"
    )


@pytest.mark.parametrize("level", range(1, 8))
def test_headings_keep_their_level(level: int):
    expected = (
        '<div role="heading" aria-level="7">Notes</div>'
        if level == 7
        else f"<h{level}>Notes</h{level}>"
    )
    assert render("#" * level + " Notes\n\nSomething.") == expected + "<p>Something.</p>"
    assert render(f"<h{level}>Notes</h{level}>") == expected


def test_headings_support_inline_formatting_and_closing_hashes():
    assert render("  ## **Notes** and `code` ###  ") == (
        "<h2><strong>Notes</strong> and <code>code</code></h2>"
    )
    assert render("<H3>*Notes* and `code`</H3>") == "<h3><em>Notes</em> and <code>code</code></h3>"
    assert render("#") == "<h1></h1>"
    assert render("## C#") == "<h2>C#</h2>"


def test_headings_end_paragraphs_and_lists():
    assert render("Before\n# Notes\nAfter\n- Item\n<h2>More</h2>") == (
        "<p>Before</p><h1>Notes</h1><p>After</p><ul><li>Item</li></ul><h2>More</h2>"
    )


@pytest.mark.parametrize("text", ["# Notes", "<h1>Notes</h1>"])
def test_headings_inside_code_remain_code(text: str):
    escaped = text.replace("<", "&lt;").replace(">", "&gt;")
    assert render(f"```\n{text}\n```") == f"<pre><code>{escaped}</code></pre>"
    assert render("    " + text) == f"<pre><code>{escaped}</code></pre>"
    assert render(f"`{text}`") == f"<p><code>{escaped}</code></p>"


@pytest.mark.parametrize(
    "text",
    [
        '<h1 onclick="alert(1)">Notes</h1>',
        "<h1>Notes</h2>",
        "<h8>Notes</h8>",
        "######## Notes",
        "#Notes",
    ],
)
def test_invalid_heading_syntax_stays_text(text: str):
    escaped = text.replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    assert render(text) == f"<p>{escaped}</p>"


def test_heading_content_cannot_inject_html():
    assert render('<h1><img src=x onerror="alert(1)"></h1>') == (
        "<h1>&lt;img src=x onerror=&quot;alert(1)&quot;&gt;</h1>"
    )
    assert render("<h7><script>alert(1)</script></h7>") == (
        '<div role="heading" aria-level="7">&lt;script&gt;alert(1)&lt;/script&gt;</div>'
    )


def test_a_link_is_a_link():
    rendered = render("See [the spec](https://modelcontextprotocol.io).")
    assert 'href="https://modelcontextprotocol.io"' in rendered
    assert 'rel="noreferrer noopener"' in rendered


def test_a_description_cannot_bring_its_own_html():
    rendered = render("<script>alert(1)</script> and <b>bold</b>")
    assert "<script" not in rendered
    assert "&lt;script&gt;" in rendered
    assert "<b>" not in rendered


def test_a_link_that_is_not_a_link_is_dropped():
    rendered = render("See [this](javascript:alert(1)).")
    assert "javascript:" not in rendered
    assert "<a " not in rendered


def test_a_quote_cannot_close_an_attribute():
    """The way an escaped code span would otherwise become a handler."""
    rendered = render('`" onmouseover="alert(1)`')
    assert '"' not in rendered.replace("&quot;", "")
    assert 'onmouseover="' not in rendered


def test_an_image_is_not_fetched():
    rendered = render("![x](https://elsewhere.example/pixel.png)")
    assert "<img" not in rendered


def test_nothing_a_server_sends_reaches_the_page_as_markup():
    attempts = [
        "<img src=x onerror=alert(1)>",
        "[x](  javascript:alert(1))",
        "<iframe src='https://elsewhere.example'></iframe>",
        "`</code><script>alert(1)</script>`",
        "**<script>alert(1)</script>**",
    ]
    for attempt in attempts:
        rendered = render(attempt).lower()
        assert "<script" not in rendered, attempt
        assert "<iframe" not in rendered, attempt
        assert "<img" not in rendered, attempt
        assert 'href="javascript:' not in rendered, attempt


def test_the_renderer_is_the_one_the_page_uses():
    """This extracts a slice of console.js. A rename there must break here
    rather than quietly leave these testing nothing."""
    source = CONSOLE.read_text(encoding="utf-8")
    assert "function markdownHtml(" in source
    assert "function prose(" in source
    assert json.dumps("prose(text, into)")  # the shape the page calls
