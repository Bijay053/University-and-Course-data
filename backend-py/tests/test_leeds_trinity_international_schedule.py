import asyncio
from unittest.mock import AsyncMock

import pytest

from app.services.scraper.central_pages import (
    _FEE_CACHE_SCHEMA_VERSION, _fee_cache_is_current,
    _parse_fee_page_html, match_central_fee,
)
from app.services.scraper.extractors.fee import extract
from app.services.scraper.international_schedule import explicit_international_fee_links

SOURCE = "https://www.leedstrinity.ac.uk/international/international-fees-and-funding/tuition-fees/"
COURSE = "https://www.leedstrinity.ac.uk/courses/undergraduate/criminology-with-foundation-year/"
# Minimal structural fixtures transcribed from the official schedule.
HTML = """
<h2>Tuition fees for the 2026-27 academic year</h2><h3>Undergraduate</h3>
<table><tr><th>Course</th><th>Tuition fee (per year)</th></tr>
<tr><td>Undergraduate courses (excluding Nursing)</td><td>£12,000</td></tr>
<tr><td>Nursing (Adult) BSc (Hons)</td><td>£14,500</td></tr>
<tr><td>Work placement year</td><td>£3,000</td></tr></table>
<h3>Postgraduate</h3><table><tr><th>Course</th><th>Tuition fees</th></tr>
<tr><td>MSc Data Science and Artificial Intelligence</td><td>£15,000</td></tr>
<tr><td>MA Creative Writing</td><td>£12,000</td></tr>
<tr><td>MA Social Work</td><td>£15,000 (fee is per year)</td></tr></table>
<h2>Previous tuition fees for international students</h2><h3>Postgraduate</h3>
<table><tr><th>Course</th><th>Tuition fees</th></tr>
<tr><td>MSc Data Science and Artificial Intelligence</td><td>£16,000</td></tr></table>
"""


def test_current_scoped_schedule_and_foundation():
    records = _parse_fee_page_html(HTML, SOURCE)
    assert len(records) == 5
    row, confidence = match_central_fee("BA (Hons) Criminology with Foundation Year", records, course_url=COURSE)
    assert confidence == "exact"
    assert (row["international_fee"], row["currency"], row["per"], row["fee_year"]) == (12000, "GBP", "year", 2026)
    assert row["source_url"] == SOURCE
    assert "excluding Nursing" in row["snippet"]


def test_exceptions_and_archived_prices_never_fuzzy_match():
    records = _parse_fee_page_html(HTML, SOURCE)
    for name, amount, period in [
        ("BSc (Hons) Nursing (Adult)", 14500, "year"),
        ("MSc Data Science and Artificial Intelligence", 15000, None),
        ("MA Creative Writing", 12000, None),
        ("MA Social Work", 15000, "year"),
    ]:
        row, confidence = match_central_fee(name, records)
        assert confidence == "exact"
        assert (row["international_fee"], row["per"]) == (amount, period)
    for name in ["MSc Data Science and Artificial Intelligence with Professional Placement",
                 "BSc Nursing (Unknown)", "MSc Creative Writing", "MA Unknown"]:
        # Wrong MA/MSc award must not borrow another award's fee.
        assert match_central_fee(name, records) == (None, "none")


def test_domestic_card_is_not_international_tuition():
    html = f"""
    <div class="lt-repo-course-fees__fees-box"><h3>UK Home fees</h3><p>£9,970</p></div>
    <div class="lt-repo-course-fees__fees-box"><h3>International fees</h3>
    <a href="{SOURCE}">More information on international tuition fees</a></div>
    <p>Tuition fees cost £9,970 a year in 2026/2027.</p>"""
    assert asyncio.run(extract(html, COURSE, country="United Kingdom")) == []
    assert explicit_international_fee_links(html, COURSE) == [SOURCE]


def test_explicit_links_are_bounded_same_origin_and_tuition_only():
    html = "".join(f'<a href="{u}">International tuition fees</a>' for u in [
        "https://evil.example/fees", "http://127.0.0.1/fees", "javascript:alert(1)",
        "/accommodation/fees", SOURCE, SOURCE + "#details",
    ])
    assert explicit_international_fee_links(html, COURSE) == [SOURCE]


def test_old_mixed_year_fee_cache_is_invalidated():
    records = _parse_fee_page_html(HTML, SOURCE)
    assert not _fee_cache_is_current({"fees": records})
    assert not _fee_cache_is_current({"fees": records, "_fee_parser_version": 1})
    assert _fee_cache_is_current({"fees": records, "_fee_parser_version": _FEE_CACHE_SCHEMA_VERSION})


def test_query_selectors_preserved_and_fragments_deduplicated():
    target = SOURCE + "?year=2027&audience=international"
    html = f'<a href="{target}#fees">International tuition fees</a><a href="{target}#more">International tuition fees</a>'
    assert explicit_international_fee_links(html, COURSE) == [target]


def test_competing_current_year_headings_fail_closed_in_either_order():
    other = HTML.replace("2026-27 academic year", "2027-28 academic year").replace("£12,000", "£13,000")
    assert _parse_fee_page_html(HTML + other, SOURCE) == []
    assert _parse_fee_page_html(other + HTML, SOURCE) == []


def test_same_year_conflicting_amounts_fail_closed():
    other = HTML.replace("£12,000", "£13,000")
    assert _parse_fee_page_html(HTML + other, SOURCE) == []


def test_challenge_and_official_non_tuition_pages_have_no_scoped_records():
    assert _parse_fee_page_html("<title>Just a moment...</title><p>Verify you are human</p>", SOURCE) == []
    assert explicit_international_fee_links(
        '<a href="//evil.example/fees">International tuition fees</a>'
        '<a href="https://www.leedstrinity.ac.uk@evil.example/fees">International tuition fees</a>'
        '<a href="/scholarships/">International tuition fees</a>'
        '<a href="/living-costs/">International tuition fees</a>'
        '<a href="/deposits/">International tuition fees</a>', COURSE
    ) == []


def test_explicit_links_have_fixed_bound():
    html = "".join(f'<a href="/fees/?year={year}">International tuition fees</a>' for year in range(2020, 2030))
    assert len(explicit_international_fee_links(html, COURSE)) == 4


def test_cache_read_respects_parser_version_and_query_identity(monkeypatch):
    from datetime import datetime, timedelta, timezone
    from types import SimpleNamespace
    from app import database
    from app.services.scraper.central_pages import _cache_get
    source = SOURCE + "?year=2026"
    row = SimpleNamespace(
        url=source, expires_at=datetime.now(timezone.utc) + timedelta(days=1),
        parsed_data={"fees": [{"international_fee": 9970}]},
    )
    session = AsyncMock()
    session.scalar.return_value = row
    context = AsyncMock()
    context.__aenter__.return_value = session
    monkeypatch.setattr(database, "AsyncSessionLocal", lambda: context)
    assert asyncio.run(_cache_get(2220, "fee_schedule", expected_url=source)) is None
    row.parsed_data = {"fees": _parse_fee_page_html(HTML, SOURCE), "_fee_parser_version": _FEE_CACHE_SCHEMA_VERSION}
    assert asyncio.run(_cache_get(2220, "fee_schedule", expected_url=source)) == row.parsed_data
    assert asyncio.run(_cache_get(2220, "fee_schedule", expected_url=SOURCE + "?year=2027")) is None


def test_fee_year_mismatch_blocks_auto_publish():
    from types import SimpleNamespace
    from app.services.auto_publish import should_auto_publish
    course = SimpleNamespace(
        completeness=100, decision_score=100,
        scrape_warnings=["international_fee_year_mismatch: selected study year 2027; published international tuition year 2026."],
    )
    decision = should_auto_publish(course)
    assert not decision.auto_publish


@pytest.mark.parametrize("existing,expected", [(None, 12000), (9970, 12000), (12500, 12500)])
def test_pipeline_correction_preservation_and_provenance(monkeypatch, existing, expected):
    from app.services.ai import gemini_client
    from app.services.scraper.extractors import fee, gemini_primary
    from app.services.scraper.extractors.base import ExtractionResult
    from app.services.scraper.pipelines.single_course import extract_course
    from app.services.scraper.config.context import set_uni_config
    from app.services.scraper.config.schema import UniConfig, ExtractionConfig
    set_uni_config(UniConfig(
        slug="leedstrinity_2220", name="Leeds Trinity University",
        base_url="https://www.leedstrinity.ac.uk", scrape_url="https://www.leedstrinity.ac.uk/courses/",
        extraction=ExtractionConfig(skip_browser_rescue=True, skip_per_course_browser=True),
    ))
    monkeypatch.setattr(gemini_primary, "extract_primary", AsyncMock(
        return_value=({}, 0.0, 0, 0, {"skipped": True})))
    skipped = gemini_client.GeminiResponse("", 0, 0, 0, skipped=True, skip_reason="offline_test")
    monkeypatch.setattr(gemini_client, "generate", AsyncMock(return_value=skipped))
    monkeypatch.setattr(gemini_client, "generate_with_images", AsyncMock(return_value=skipped))
    if existing is not None:
        monkeypatch.setattr(fee, "extract", AsyncMock(return_value=[ExtractionResult(
            field_key="international_fee", value=existing,
            normalized={"international_fee": existing, "currency": "GBP", "fee_term": "year", "fee_year": 2027},
            confidence=0.99, snippet="Test pre-existing deterministic field", method="fee.strong_label",
        )]))
    course_html = f"""
    <h1>BA (Hons) Criminology with Foundation Year</h1>
    <div class="lt-section-course-details__year-cta">2027</div>
    <div class="lt-repo-course-fees__fees-box"><h3>UK Home fees</h3><p>£9,970</p></div>
    <div class="lt-repo-course-fees__fees-box"><h3>International fees</h3>
    <a href="{SOURCE}">More information on international tuition fees</a></div>
    <p>Full-time four year undergraduate degree. September entry.</p>
    """
    result = asyncio.run(extract_course(
        COURSE, html=course_html, country="United Kingdom", use_ai_fallback=False,
        central_data={"fees": _parse_fee_page_html(HTML, SOURCE), "fee_page_url": SOURCE},
    ))
    payload = result["payload"]
    assert payload["international_fee"] == expected
    if expected == 12000:
        assert payload["fee_year"] == 2026
        assert any(w.startswith("international_fee_year_mismatch:") for w in payload["scrape_warnings"])
        assert any(e["field_key"] == "international_fee" and e.get("source_url") == SOURCE
                   and e.get("method") == "central_page:fees:exact" for e in result["evidence"])
    else:
        assert payload["fee_year"] == 2027