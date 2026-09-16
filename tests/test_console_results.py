"""What the console shows for one tool result.

The panel reads a value out of whatever the server sent: `structuredContent`
where the revision has it, and the text itself where it does not, because the
two revisions older than 2025-06-18 have no such field and a tool of those
revisions puts the same data in the text.

Reading text as JSON is a guess, and the cases that matter are the ones nobody
types by hand: a tool that answers `5`, or `true`, or `null`. These check that
a one-word answer stays the word.

Skipped where `node` is absent: the decision is JavaScript, and running it
under anything else would test a translation.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

NODE = shutil.which("node")
CONSOLE = Path(__file__).parent.parent / "src" / "aiohttp_tiny_mcp" / "console" / "console.js"

pytestmark = [
    pytest.mark.skipif(NODE is None, reason="node is not installed"),
    pytest.mark.timeout(10),
]


def views(result: dict, text: str) -> dict:
    """Ask the page's own function what it would show."""
    source = CONSOLE.read_text(encoding="utf-8")
    start = source.index("function parsed(text)")
    end = source.index("function report(title, body, failed)")
    driver = (
        source[start:end]
        + f"\nconst shown = resultViews({json.dumps(result)}, {json.dumps(text)});\n"
        + "process.stdout.write(JSON.stringify(shown));\n"
    )
    finished = subprocess.run([NODE, "-e", driver], capture_output=True, text=True, check=True)
    return json.loads(finished.stdout)


def test_a_sentence_stays_a_sentence():
    shown = views({}, "debug is now True")
    assert shown["text"] == "debug is now True"
    assert "value" not in shown or shown["value"] is None


def test_one_word_stays_the_word():
    """`deployed` does not parse, so there is nothing to guess about."""
    shown = views({}, "deployed web")
    assert shown["text"] == "deployed web"
    assert shown.get("value") is None


def test_a_number_in_words_is_not_a_tree():
    """`"5"` parses, to the number five. A number is not a tree, and a tool
    that answered `5` meant five."""
    shown = views({}, "5")
    assert shown["text"] == "5"
    assert shown.get("value") is None


def test_true_and_null_are_not_trees():
    for text in ("true", "false", "null"):
        shown = views({}, text)
        assert shown["text"] == text, text
        assert shown.get("value") is None, text


def test_a_quoted_string_is_not_a_tree():
    """Valid JSON, and still one value rather than a structure."""
    shown = views({}, '"a quoted string"')
    assert shown["text"] == '"a quoted string"'
    assert shown.get("value") is None


def test_nothing_at_all_shows_nothing():
    shown = views({}, "")
    assert shown["text"] is None
    assert shown.get("value") is None


def test_structured_content_becomes_the_value():
    shown = views({"structuredContent": {"total": 5}}, '{"total": 5}')
    assert shown["value"] == {"total": 5}
    assert shown["text"] is None
    assert shown["label"] == "Result"


def test_text_that_is_json_becomes_the_value_without_structured_content():
    """What the revisions older than 2025-06-18 send."""
    shown = views({}, '{"total": 5}')
    assert shown["value"] == {"total": 5}
    assert shown["text"] is None


def test_a_json_array_becomes_the_value():
    shown = views({}, "[1, 2, 3]")
    assert shown["value"] == [1, 2, 3]
    assert shown["text"] is None


def test_text_that_disagrees_with_the_structure_is_kept():
    """Both are shown, because they are not the same thing and dropping
    either would hide what the server actually sent."""
    shown = views({"structuredContent": {"total": 5}}, "counted five things")
    assert shown["text"] == "counted five things"
    assert shown["value"] == {"total": 5}
    assert shown["label"] == "Structured content"


def test_broken_json_stays_text():
    shown = views({}, '{"total": ')
    assert shown["text"] == '{"total": '
    assert shown.get("value") is None
