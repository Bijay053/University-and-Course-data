from datetime import date

import pytest

from app.services.scraper.extractors.ulaw_fees import (
    apply_course_fee_authority,
    parse_course_fees,
)

URL = "https://www.law.ac.uk/study/postgraduate/business/msc-healthcare-management/"
TODAY = date(2026, 9, 25)


def page(rows, title="MSc Healthcare Management", label="International Students"):
    return f"""<html><main><h1>{title}</h1>
    <a role="tab" href="#home">UK Students</a>
    <div id="home"><p>2026/27 Course Fees</p><p>London: £9,790</p></div>
    <a role="tab" href="#fees">{label}</a>
    <div id="fees"><table>{rows}</table></div></main></html>"""


def heading(year=2026):
    return f"<tr><td>{year}/{str(year+1)[2:]} Course Fees</td><td></td></tr>"


def row(campus, price):
    return f"<tr><td>{campus}</td><td>{price}</td></tr>"


def test_table_gross_price_campus_year_and_route_are_not_flattened():
    html = page(
        heading() + row("London", "£19,050 (or £16,050 including a £3,000 International Bursary*)")
        + row("Outside London", "£17,500")
        + row("2-year programme with Professional Practice", "")
        + row("London", "£20,950") + heading(2027) + row("London", "£19,600")
    )
    out = parse_course_fees(html, URL, today=TODAY)
    assert out["international_fee"] is None
    assert out["status"] == "range"
    assert [x["amount"] for x in out["selected"]] == [19050, 17500]
    assert out["fee_year"] == 2026
    assert out["fee_term"] == "Full Course"
    assert all(x["source_url"] == URL for x in out["options"])
    assert all("9,790" not in x["snippet"] for x in out["options"])
    assert len(out["options"]) == 4


def test_uniform_is_scalar_and_authority_clears_stale_companions():
    html = page(heading() + row("London", "£20,600") + row("Outside London", "£20,600"))
    payload = {"international_fee": 16900, "fee_year": 2027, "fee_term": "Annual", "currency": "AUD"}
    evidence = [{"field_key": "international_fee", "value": 16900, "snippet": "Domestic students"}]
    apply_course_fee_authority(html, URL, payload, evidence)
    assert payload == {"international_fee": 20600, "fee_year": 2026, "fee_term": "Full Course", "currency": "GBP"}
    assert len(evidence) == 1
    assert evidence[0]["method"] == "fee.ulaw_course_authority"


def test_legal_awards_do_not_borrow_diploma_certificate_prices():
    html = page(
        heading() + row("LLM Master of Laws (General)", "")
        + row("London", "£19,600") + row("Outside London", "£18,250")
        + row("Postgraduate Diploma Law (General)", "") + row("London", "£13,150")
        + row("Postgraduate Certificate Law (General)", "") + row("London", "£6,600"),
        title="LLM Master of Laws (General)",
    )
    out = parse_course_fees(html, URL.replace("business/msc-healthcare-management", "law/llm-master-of-laws-general"), today=TODAY)
    assert [o["amount"] for o in out["selected"]] == [19600, 18250]


def test_ug_annual_is_explicit_foundation_is_not_borrowed():
    url = URL.replace("postgraduate/business/msc-healthcare-management", "undergraduate/law/llb-hons-law")
    html = page(heading() + row("London", "£18,100 per year") + row("Outside London", "£17,200 per year"), title="LLB Law")
    assert parse_course_fees(html, url, today=TODAY)["fee_term"] == "Annual"
    foundation = html.replace("<h1>LLB Law</h1>", "<h1>LLB Law with Foundation Year</h1>")
    assert parse_course_fees(foundation, url, today=TODAY)["status"] == "unresolved"
    explicit = page(
        heading() + row("Foundation Year", "") + row("London", "£18,100 per year")
        + row("Outside London", "£18,100 per year"),
        title="LLB Law with Foundation Year",
    )
    assert parse_course_fees(explicit, url, today=TODAY)["international_fee"] == 18100


def test_legacy_list_audience_ends_before_domestic_and_next_cohort():
    html = """<h1>LLB Law with Criminology</h1>
    <a role="tab" href="#f">Fees and Funding</a><div id="f">
    <p>2026/27 Course Fees</p><p>UK students</p><ul><li>London: £9,790</li></ul>
    <p>International Students per year:</p><ul><li>London: £18,100 (or £15,600 including a £2,500 bursary)</li>
    <li>Non-London: £17,200</li></ul>
    <p>2027/28 Course Fees</p><p>UK students</p><ul><li>London: £10,050</li></ul>
    <p>International Students per year:</p><ul><li>London: £18,650</li></ul></div>"""
    out = parse_course_fees(html, URL.replace("postgraduate/business/msc-healthcare-management", "undergraduate/law/llb-hons-law-with-criminology"), today=TODAY)
    assert [o["amount"] for o in out["selected"]] == [18100, 17200]
    assert out["fee_term"] == "Annual"


def test_explicit_open_ended_cohort_retains_published_year():
    html = page(heading(2025).replace("Course Fees", "Course Fees (for courses starting on or after 1 July 2025)") + row("London", "£18,500"))
    out = parse_course_fees(html, URL, today=TODAY)
    assert out["fee_year"] == 2025
    assert out["international_fee"] == 18500
    stale = html.replace(" (for courses starting on or after 1 July 2025)", "")
    assert parse_course_fees(stale, URL, today=TODAY)["status"] == "unresolved"


def test_academic_cohort_date_window_wins_over_calendar_year():
    html = page(
        heading().replace("Course Fees", "Course Fees (for courses starting between 1 July 2026 - 31 May 2027)")
        + row("London", "£19,050")
        + heading(2027).replace("Course Fees", "Course Fees (for courses starting between 1 June 2027 - 31 May 2028)")
        + row("London", "£19,600")
    )
    out = parse_course_fees(html, URL, today=date(2027, 4, 1))
    assert out["international_fee"] == 19050
    assert out["fee_year"] == 2026


@pytest.mark.parametrize("url", [
    URL.replace("www.law.ac.uk", "example.edu"),
    URL.replace("www.law.ac.uk", "www.law.ac.uk.example.com"),
    "https://www.law.ac.uk/study/fees-and-funding/",
    URL + "online/",
])
def test_host_and_course_ownership_isolation(url):
    assert parse_course_fees(page(heading() + row("London", "£18,100")), url) is None


def test_domestic_only_and_incidental_surcharge_do_not_become_international():
    html = page(heading() + row("London", "£9,790"), label="UK Students")
    html += "<p>International graduate visa health surcharge £2,070</p>"
    assert parse_course_fees(html, URL) is None


@pytest.mark.asyncio
async def test_full_extract_reapplies_owned_authority_after_all_fallbacks(monkeypatch):
    from app.services.scraper.config.context import current_uni_config
    from app.services.scraper.config.loader import load_uni_config
    from app.services.scraper.pipelines.single_course import extract_course

    async def no_ai(*args, **kwargs):
        return {}, 0.0, 0, 0, {"skipped": True}

    monkeypatch.setattr("app.services.scraper.extractors.gemini_primary.extract_primary", no_ai)
    cfg = load_uni_config(slug="law_1902", name="University of Law",
                          scrape_url="https://www.law.ac.uk/study/", create_missing_stub=False)
    token = current_uni_config.set(cfg)
    try:
        html = page(heading() + row("London", "£20,600") + row("Outside London", "£20,600"))
        out = await extract_course(URL, html=html, country="United Kingdom", use_ai_fallback=False)
        assert out["payload"]["international_fee"] == 20600
        assert out["payload"]["fee_term"] == "Full Course"
        assert out["payload"]["extraction_method"]["fee_variants"]["status"] == "uniform"
        assert all("Domestic students" not in e["snippet"] for e in out["evidence"] if e["field_key"] == "international_fee")
    finally:
        current_uni_config.reset(token)


def test_validated_range_does_not_hide_corrupt_or_detached_fee_metadata():
    from copy import deepcopy
    from app.services.scraper.extractors.ulaw_fees import METHOD, validated_fee_variants
    from app.services.scraper.data_quality import _check_course

    payload = {"course_website": URL}
    result = apply_course_fee_authority(
        page(heading() + row("London", "£19,050") + row("Outside London", "£17,500")),
        URL, payload, [],
    )
    payload["extraction_method"] = {"international_fee": METHOD, "fee_variants": result}
    assert validated_fee_variants(payload)
    for key, value in [
        ("course_website", URL.replace("www.law.ac.uk", "other.edu")),
        ("fee_year", 2025), ("fee_term", "Annual"), ("currency", "AUD"),
        ("international_fee", 16900),
    ]:
        changed = {**payload, key: value}
        assert validated_fee_variants(changed) is None
    changed = deepcopy(payload)
    changed["extraction_method"]["fee_variants"]["selected"][0]["source_url"] = URL + "online/"
    assert validated_fee_variants(changed) is None
    codes = {issue.code for issue in _check_course(changed, URL)}
    assert "missing_international_fee" in codes


def test_range_recovery_preserves_existing_low_fee_quality_checks():
    from app.services.scraper.extractors.ulaw_fees import METHOD
    from app.services.scraper.data_quality import _check_course

    payload = {"course_website": URL, "duration": 12, "duration_term": "months"}
    result = apply_course_fee_authority(
        page(heading() + row("London", "£100") + row("Outside London", "£200")),
        URL, payload, [],
    )
    payload["extraction_method"] = {"international_fee": METHOD, "fee_variants": result}
    codes = {issue.code for issue in _check_course(payload, URL)}
    assert "international_fee_campus_review" in codes
    assert "fee_too_low" in codes
    assert "missing_international_fee" not in codes


@pytest.mark.asyncio
async def test_live_fee_only_shortcut_is_bounded_and_requires_complete_authority(monkeypatch):
    import httpx
    from app.services.scraper.extractors.ulaw_fees import recover_course_fee_only, validated_fee_variants

    html = page(heading() + row("London", "£19,050") + row("Outside London", "£17,500"))
    requests = []
    def transport(request):
        requests.append(str(request.url))
        return httpx.Response(200, text=html)

    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(
        **kwargs, transport=httpx.MockTransport(transport),
    ))
    out = await recover_course_fee_only(URL)
    assert validated_fee_variants(out["payload"])
    assert len(requests) == 1
    assert set(out["payload"]) <= {
        "international_fee", "fee_year", "fee_term", "currency",
        "course_website", "extraction_method",
    }
    html = page(heading() + row("London", "£9,790"), label="UK Students")
    assert await recover_course_fee_only(URL) is None
    assert await recover_course_fee_only(URL.replace("www.law.ac.uk", "other.edu")) is None
    assert len(requests) == 2


@pytest.mark.asyncio
async def test_quality_endpoint_marks_persisted_range_recovered_but_review_required():
    from collections import defaultdict
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from app.routers.scrape import get_course_quality_scores
    from app.services.scraper.extractors.ulaw_fees import METHOD

    stored = defaultdict(lambda: None, {"id": 1, "course_website": URL})
    result = apply_course_fee_authority(
        page(heading() + row("London", "£19,050") + row("Outside London", "£17,500")),
        URL, stored, [],
    )
    stored["extraction_method"] = {"international_fee": METHOD, "fee_variants": result}
    db = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(
        mappings=lambda: SimpleNamespace(all=lambda: [stored]),
    )))
    result = await get_course_quality_scores(92, db)
    fee = result["courses"][0]["breakdown"]["fee"]
    codes = {issue["code"] for issue in result["courses"][0]["issues"]}
    assert "missing_international_fee" not in codes
    assert "international_fee_campus_review" in codes
    assert fee["quality"] != "missing"