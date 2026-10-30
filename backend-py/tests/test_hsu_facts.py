from app.services.scraper.extractors.hsu_facts import apply_course_authority
import pytest

URL = "https://www.hsu.ac.uk/course/msc-chiropractic-pre-registration/"
LOCATION = """<nav>Bournemouth Campus London Campus Reading Campus</nav>
<article><span class="label">Location</span><span class="value">Bournemouth</span></article>"""
FEES = """<h3>Tuition Fees 2025/26</h3><h4>International</h4>
<p>£19,755 per annum (Full time)</p>
<h3>Tuition Fees 2026/27</h3><h4>UK/ROI/Channel Islands</h4>
<p>£9,790 (Full time)</p><h4>International</h4>
<p>£17, 684 (Full time)</p><p>£1956 per 10 credits (Part-time)</p>"""


def apply(html, url=URL):
    payload = {"course_location": "Bournemouth Campus London Campus Reading Campus",
               "international_fee": 9790, "currency": "AUD", "fee_term": "Annual"}
    evidence = [{"field_key": "international_fee", "value": 9790}]
    apply_course_authority(html, url, payload, evidence)
    return payload, evidence


def test_current_international_fee_and_course_location_replace_contamination():
    payload, evidence = apply(LOCATION + FEES)
    assert payload["course_location"] == "Bournemouth"
    assert payload["international_fee"] == 17684
    assert payload["currency"] == "GBP"
    assert payload["fee_year"] == 2026
    assert payload["fee_term"] == "Annual"
    assert evidence[0]["decision_status"] == "needs_review"


def test_unpublished_overseas_price_is_not_domestic_price():
    payload, _ = apply(LOCATION + """<h3>Tuition Fees 2027/28</h3>
    <h4>UK/ROI/Channel Islands</h4>
    <p>£10,050 Fees for overseas students are still to be confirmed.</p>""")
    assert payload["international_fee"] is None
    assert payload["fee_term"] is None


def test_latest_unpublished_section_does_not_reuse_older_fee():
    payload, _ = apply(FEES + "<h3>Tuition Fees 2027/28</h3><h4>International</h4><p>To be confirmed</p>")
    assert payload["international_fee"] is None


def test_navigation_and_absent_international_evidence_fail_closed():
    payload, _ = apply("<nav>London Campus</nav>")
    assert payload["course_location"] is None
    assert payload["international_fee"] is None


def test_other_hosts_and_noncourse_pages_untouched():
    for url in ("https://other.ac.uk/course/test/", "https://www.hsu.ac.uk/study/"):
        payload, _ = apply(LOCATION + FEES, url)
        assert payload["international_fee"] == 9790
        assert payload["currency"] == "AUD"


def test_explicit_total_is_not_annualised():
    payload, _ = apply(LOCATION + "<h3>Tuition Fees 2026/27</h3><h4>International</h4><p>£24,000 total course fee</p>")
    assert payload["fee_term"] == "Full Course"


def test_conflicting_current_prices_are_not_guessed():
    payload, _ = apply(FEES + "<p>£20,000 (Full time)</p>")
    assert payload["international_fee"] is None


@pytest.mark.asyncio
async def test_full_pipeline_preserves_hsu_authority_and_selected_evidence(monkeypatch):
    from app.services.scraper.config.context import current_uni_config
    from app.services.scraper.config.loader import load_uni_config
    from app.services.scraper.pipelines.single_course import extract_course

    async def no_ai(*args, **kwargs):
        return {}, 0.0, 0, 0, {"skipped": True}

    monkeypatch.setattr("app.services.scraper.extractors.gemini_primary.extract_primary", no_ai)
    config = load_uni_config(
        slug="hsu", name="Health Sciences University", university_id=101,
        scrape_url="https://www.hsu.ac.uk", create_missing_stub=False,
    )
    config.extraction.skip_per_course_browser = True
    token = current_uni_config.set(config)
    try:
        html = """<main><h1>MSc Chiropractic (Pre-registration)</h1>
        <p>Full-time on campus. Duration: 2 years. Start date: September.
        IELTS 7.0 with no band below 6.5.</p>""" + LOCATION + FEES + "</main>"
        result = await extract_course(
            URL, html=html, country="United Kingdom", use_ai_fallback=False,
        )
    finally:
        current_uni_config.reset(token)
    assert result["payload"]["course_location"] == "Bournemouth"
    assert result["payload"]["international_fee"] == 17684
    assert result["payload"]["currency"] == "GBP"
    assert result["payload"]["fee_year"] == 2026
    assert result["payload"]["fee_term"] == "Annual"
    for field, method in (
        ("international_fee", "fee.hsu_international_tuition"),
        ("course_location", "location.hsu_course_fact"),
    ):
        assert any(
            row["field_key"] == field and row.get("method") == method
            and row.get("decision_status") == "selected"
            for row in result["evidence"]
        )
