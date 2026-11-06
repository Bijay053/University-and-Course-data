from types import SimpleNamespace
import pytest
from app.services.scrape_assistant import known_sources
from app.services.scrape_assistant_coverage import research_coverage
from app.services import scrape_assistant_tools as tools


def context(**english):
    return {"home": "https://example.edu", "config": SimpleNamespace(
        extraction=SimpleNamespace(english=SimpleNamespace(**english)),
        discovery=SimpleNamespace(seed_urls=[]))}


def rows(count):
    return [{"id": i, "name": f"Degree {i}", "url": f"https://example.edu/course/{i}"} for i in range(count)]


def test_english_audit_does_not_stop_after_three_pages():
    cohort = rows(20)
    urls, _ = known_sources("Check IELTS overall and band scores against the official English requirements page.",
                            context(central_page=None), cohort)
    assert len(urls) == 21
    assert all(row["url"] in urls for row in cohort)
    assert "https://example.edu" in urls


def test_level_specific_central_sources_are_included():
    urls, _ = known_sources("Verify English", context(central_page=None,
                           central_page_ug="https://example.edu/ug-english",
                           central_page_pg="https://example.edu/pg-english"), rows(20))
    assert urls[:2] == ["https://example.edu/ug-english", "https://example.edu/pg-english"]
    assert len(urls) == 22


def test_page_fetch_is_not_claimed_as_ielts_verification():
    coverage = research_coverage(rows(4), [
        {"url": "https://example.edu/course/0/", "verified": True},
        {"url": "https://example.edu/course/1", "verified": False},
        {"url": "https://example.edu/english", "verified": True}])
    assert (coverage["checked"], coverage["failed"], coverage["unchecked"]) == (1, 1, 2)
    assert "Course-page coverage: 1/4 read successfully" in coverage["summary"]
    assert "verified" not in coverage["summary"].lower()


def test_empty_and_bounded_reviews():
    assert research_coverage([], [])["total"] == 0
    urls, _ = known_sources("Check IELTS", context(central_page=None), rows(100))
    assert len(urls) == 44
    coverage = research_coverage(rows(100), [{"url": u, "verified": True} for u in urls])
    assert coverage["unchecked"] == 57


def test_failed_pdf_is_counted_even_without_matching_course_rows():
    url = "https://www.lsbu.ac.uk/_media_old/pdf/guidelines/Eng-lang-requirements.pdf.pdf.pdf"
    coverage = research_coverage([], [{
        "url": url, "verified": False, "sourceType": "pdf",
        "error": "robots.txt could not be verified (HTTP 403)",
    }])
    assert coverage["total"] == 0  # No course pages were requested.
    assert coverage["sourceChecks"]["failed"] == 1
    assert coverage["sourceChecks"]["pdf"] == {"total": 1, "checked": 0, "failed": 1}
    assert "PDF checks: 0/1" in coverage["summary"]
    assert "0/0" not in coverage["summary"]
    assert "403" in coverage["sourceChecks"]["rows"][0]["error"]


def test_readable_central_pdf_does_not_mark_253_courses_checked():
    coverage = research_coverage(rows(253), [{
        "url": "https://example.edu/english.pdf", "verified": True, "sourceType": "pdf",
    }])
    assert coverage["checked"] == 0
    assert coverage["unchecked"] == 253
    assert coverage["sourceChecks"]["pdf"]["checked"] == 1
    assert "applicability to every course" in coverage["summary"]


def test_mixed_sources_and_fragment_aliases_have_conserved_counts():
    coverage = research_coverage(rows(1), [
        {"url": "https://example.edu/course/0#requirements", "verified": True},
        {"url": "https://example.edu/english.pdf#page=1", "sourceType": "pdf", "verified": True},
        {"url": "https://example.edu/english.pdf", "sourceType": "pdf", "verified": False},
    ])
    assert coverage["checked"] == 1
    assert coverage["sourceChecks"]["total"] == 2
    assert coverage["sourceChecks"]["checked"] == 1
    assert coverage["sourceChecks"]["failed"] == 1
    assert coverage["sourceChecks"]["pdf"]["failed"] == 1


@pytest.mark.parametrize("overall", [6.0, 6.5])
def test_lsbu_indexed_component_phrase_parses_all_four_bands(overall):
    # Indexed text is a parser regression fixture, not a live-verified profile.
    # Its headers exclude Health courses and postgraduate research degrees.
    from app.services.scraper.extractors.english_test import _ielts
    result = _ielts(f"IELTS Academic Overall score: {overall} (with no component below 5.5)")
    assert result["overall"] == overall
    for component in ("listening", "reading", "speaking", "writing"):
        assert result[component] == 5.5


@pytest.mark.asyncio
async def test_english_link_discovery_rejects_external_and_private_targets(monkeypatch):
    html = ('<html><title>University</title><body>' + 'Official university information. ' * 10 +
            '<a href="/admissions/english-requirements">English requirements</a>'
            '<a href="https://evil.test/english">English</a>'
            '<a href="http://127.0.0.1/english">English</a></body></html>')
    async def request(*args, **kwargs):
        return html.encode()
    async def extractor(*args, **kwargs):
        return []
    from app.services.scraper.extractors import fee, english_test
    monkeypatch.setattr(tools, "public_request", request)
    monkeypatch.setattr(fee, "extract", extractor)
    monkeypatch.setattr(english_test, "extract", extractor)
    result = await tools.inspect_source("https://example.edu/", "https://example.edu")
    assert result["englishLinks"] == ["https://example.edu/admissions/english-requirements"]
