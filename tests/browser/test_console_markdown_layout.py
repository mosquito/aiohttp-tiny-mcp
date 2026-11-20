from pathlib import Path

import pytest
from playwright.async_api import BrowserContext, expect

CONSOLE = Path(__file__).resolve().parents[2] / "src" / "aiohttp_tiny_mcp" / "console"

pytestmark = [pytest.mark.timeout(30), pytest.mark.asyncio(loop_scope="session")]


async def test_headings_have_distinct_sizes_in_descriptions_and_dialogs(
    context: BrowserContext,
) -> None:
    source = (CONSOLE / "console.js").read_text()
    renderer = source[source.index("const SAFE_LINK") : source.index("function prose(")]
    page = await context.new_page()
    await page.set_viewport_size({"width": 390, "height": 844})
    await page.set_content(
        '<div class="about"></div>'
        '<dialog class="question" open><h2>Dialog title</h2>'
        '<div id="question-text"></div></dialog>'
    )
    await page.add_style_tag(path=str(CONSOLE / "console.css"))
    await page.add_script_tag(content=renderer)
    await page.evaluate(
        "() => {const text = Array.from({length: 7}, (_, index) => "
        "'#'.repeat(index + 1) + ' Level ' + (index + 1)).join('\\n');"
        "for (const target of document.querySelectorAll('.about, #question-text')) "
        "target.innerHTML = markdownHtml(text);}"
    )
    for selector in (".about", "#question-text"):
        target = page.locator(selector)
        sizes: list[float] = []
        for level in range(1, 8):
            heading = target.get_by_role("heading", level=level)
            await expect(heading).to_have_text(f"Level {level}")
            sizes.append(
                await heading.evaluate("node => parseFloat(getComputedStyle(node).fontSize)")
            )
        assert all(left > right for left, right in zip(sizes, sizes[1:], strict=False))
        assert (
            await target.get_by_role("heading", level=1).evaluate(
                "node => getComputedStyle(node).marginTop"
            )
            == "0px"
        )
        assert await target.evaluate("node => node.scrollWidth <= node.clientWidth")
    await expect(page.get_by_text("Dialog title", exact=True)).to_have_css("font-size", "15px")
    await page.close()
