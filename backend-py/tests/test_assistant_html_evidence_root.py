"""A decorative article must not hide readable sibling course information."""
from unittest.mock import AsyncMock

import pytest

from app.services.scrape_assistant_tools import inspect_source

URL = "https://uni.example/study/courses/aerospace-engineering"
COURSE = (
    "<h1>Aerospace Engineering BEng (Hons)</h1>"
    "<p>Study aerospace engineering over four years including an optional placement. "
    "International first-year tuition is GBP 22,575 for 2027. "
    "IELTS Academic 6.0 overall, with at least 5.5 in reading, writing, listening and speaking. "
    "The programme is offered at our city campus.</p>"
)


@pytest.mark.asyncio
@pytest.mark.parametrize("placeholder", [
    "<article class='course-mobile-image-change'><img src='/hero.png'></article>",
    "<main></main>",
    "<article>Course image</article>",
])
async def test_readable_sibling_details_are_not_an_access_failure(monkeypatch, placeholder):
    from app.services.scraper.extractors import fee, english_test
    monkeypatch.setattr(fee, "extract", AsyncMock(return_value=[]))
    monkeypatch.setattr(english_test, "extract", AsyncMock(return_value=[]))
    html = ("<html><title>Aerospace Engineering</title><body>"
            "<nav>Not course evidence " + "IELTS 9.0 " * 30 + "</nav>"
            + placeholder + "<section>" + COURSE + "</section>"
            "<footer>Unrelated site links</footer></body></html>")
    result = await inspect_source(URL, "https://uni.example", raw_body=html.encode())
    assert result["verified"]
    assert "Aerospace Engineering BEng (Hons)" in result["_fullText"]
    assert "IELTS Academic 6.0" in result["_fullText"]
    assert "IELTS 9.0" not in result["_fullText"]
    assert "Unrelated site links" not in result["_fullText"]


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [
    "<article><img src='/hero.png'></article><nav>" + COURSE * 5 + "</nav>",
    "<main>Just a moment</main><section>" + COURSE * 5 + "</section>",
    "<main>Access denied</main><section>" + COURSE * 5 + "</section>",
])
async def test_empty_shells_and_explicit_challenges_remain_unverified(body):
    result = await inspect_source(
        URL, "https://uni.example", raw_body=("<html><body>" + body + "</body></html>").encode()
    )
    assert not result["verified"]
    assert result["failureStage"] == "content_validation"


@pytest.mark.asyncio
async def test_substantive_hero_article_does_not_hide_sibling_entry_requirements(monkeypatch):
    from app.services.scraper.extractors import fee, english_test
    monkeypatch.setattr(fee, "extract", AsyncMock(return_value=[]))
    monkeypatch.setattr(english_test, "extract", AsyncMock(return_value=[]))
    html = ("<article><h1>Aerospace Engineering BEng (Hons)</h1>"
            "<p>" + "Study aerospace engineering at our city campus. " * 8 + "</p></article>"
            "<section><h2>Entry requirements</h2>"
            "<p>IELTS Academic 6.0 overall with no components below 5.5.</p></section>")
    result = await inspect_source(URL, "https://uni.example", raw_body=html.encode())
    assert result["verified"]
    assert "Entry requirements" in result["_fullText"]
    assert "IELTS Academic 6.0" in result["_fullText"]
