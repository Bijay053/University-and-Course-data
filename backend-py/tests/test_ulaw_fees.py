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


# The entire authorized 85-row job, not a hand-picked sample. These are full
# official HTML responses captured once on 2026-09-25 (no network in tests).
# Amount expectations below are reviewed against the published fee panels.
def _population():
    import gzip
    import json
    from pathlib import Path
    path = Path(__file__).parent / "fixtures" / "ulaw_live_85_20260925.json.gz"
    with gzip.open(path, "rt") as handle:
        return {row["id"]: row for row in json.load(handle)["rows"]}


POPULATION = _population()
EXPECTED = {}
for ids, amounts, period, year in [
    ([40987, 40988, 40989, 40990, 40991, 41008, 41026, 41030, 41036, 41037, 41038, 41064, 41065], [18100, 17200], "Annual", 2026),
    ([40992], [22800, 20950], "Annual", 2026),
    ([41022, 41024, 41025], [17550, 16700], "Annual", 2026),
    ([41023], [18100, 17200], "Annual", 2027),
    ([40993, 40994, 40995, 40996, 40997, 40998, 40999, 41000, 41002, 41009,
      41010, 41011, 41012, 41013, 41014, 41015, 41016, 41027, 41028, 41034,
      41040, 41041, 41042, 41043, 41044, 41045, 41046, 41047, 41048, 41049,
      41050, 41051, 41057, 41058, 41059, 41060], [19600, 18250], "Full Course", 2026),
    ([41003], [13150, 12200, 6600, 6150], "Full Course", 2026),
    ([41004, 41005, 41006, 41007, 41029, 41031, 41032, 41054, 41071], [19050, 17500], "Full Course", 2026),
    ([41017], [18850, 15150], "Full Course", 2026),
    ([41018], [20550, 17400], "Full Course", 2026),
    ([41019], [15450, 12450], "Full Course", 2026),
    ([41033], [18500, 17000], "Full Course", 2025),
    ([41035, 41039, 41053, 41063, 41067], [17500, 16500], "Full Course", 2026),
    ([41052], [17550, 16700], "Full Course", 2026),
    ([41055, 41068, 41069, 41070], [20600, 20600], "Full Course", 2026),
    ([41056], [20100, 18700], "Full Course", 2027),
    ([41061], [14150, 11350], "Full Course", 2026),
    ([41062], [18100, 17200], "Full Course", 2026),
    ([41066], [18650, 17700], "Full Course", 2027),
]:
    for course_id in ids:
        assert course_id not in EXPECTED
        EXPECTED[course_id] = (amounts, period, year)

UNRESOLVED = {
    41001: "LPC is closed to new applications and publishes no current tuition",
    41020: "SQE1 preparation tuition has no explicit international applicability",
    41021: "SQE2 preparation tuition has no explicit international applicability",
}
assert len(POPULATION) == 85
assert set(EXPECTED) | set(UNRESOLVED) == set(POPULATION)
assert len(EXPECTED) == 82


@pytest.mark.parametrize("course_id", sorted(POPULATION))
def test_complete_frozen_ulaw_population(course_id):
    from app.services.scraper.extractors.ulaw_fees import METHOD, validated_fee_variants
    from app.services.scraper.data_quality import _check_course

    row = POPULATION[course_id]
    result = parse_course_fees(row["html"], row["url"], today=TODAY)
    if course_id in UNRESOLVED:
        assert result is None, UNRESOLVED[course_id]
        return
    amounts, term, year = EXPECTED[course_id]
    assert [o["amount"] for o in result["selected"]] == amounts
    assert result["fee_term"] == term
    assert result["fee_year"] == year
    uniform = len(set(amounts)) == 1
    assert result["status"] == ("uniform" if uniform else "range")
    assert result["international_fee"] == (amounts[0] if uniform else None)
    assert result["currency"] == "GBP"
    assert all(o["source_url"] == row["url"] and o["snippet"] for o in result["options"])
    stored = {
        "course_website": row["url"], **{k: result[k] for k in
        ("international_fee", "fee_year", "fee_term", "currency")},
        "extraction_method": {"international_fee": METHOD, "fee_variants": result},
    }
    assert validated_fee_variants(stored) == result
    codes = {issue.code for issue in _check_course(stored, row["url"])}
    assert "missing_international_fee" not in codes
    assert "fee_too_low" not in codes
    assert ("international_fee_campus_review" in codes) == (not uniform)


def test_live_population_preserves_campus_award_numeric_and_year_boundaries():
    def parsed(course_id):
        row = POPULATION[course_id]
        return parse_course_fees(row["html"], row["url"], today=TODAY)
    assert [o["campus"] for o in parsed(40990)["selected"]] == ["London", "Non London"]
    assert [o["campus"] for o in parsed(41052)["selected"]] == ["London Bloomsbury", "Birmingham/Manchester"]
    assert {o["study_variant"] for o in parsed(41003)["selected"]} == {"PG Dip", "PG Cert"}
    assert {o["study_variant"] for o in parsed(41011)["selected"]} == {"LLM Master of Law (International)"}
    maritime = parsed(41051)
    assert [o["amount"] for o in maritime["selected"]] == [19600, 18250]
    assert [(o["year"], o["amount"]) for o in maritime["options"]] == [
        (2026, 19600), (2026, 18250), (2027, 20100), (2027, 18700),
    ]
    assert [o["amount"] for o in parsed(41063)["selected"]] == [17500, 16500]


def test_bursary_applicability_is_course_and_year_scoped_not_a_global_default():
    for course_id in (41017, 41019, 41061):
        source = POPULATION[course_id]
        result = parse_course_fees(source["html"], source["url"], today=TODAY)
        assert {o["year"] for o in result["options"]} == {2026}
        # Future unlabeled prices do not inherit a prior cohort's evidence.
        future = parse_course_fees(source["html"], source["url"], today=date(2028, 9, 25))
        assert future["status"] == "unresolved"
    html = page(heading() + row("London", "£6,500"), label="Fees")
    html += "<p>International scholarships and bursaries available</p><p>SRA exam fee £2,070</p>"
    assert parse_course_fees(html, URL, today=TODAY) is None


def test_top_up_period_does_not_borrow_an_unrelated_courses_duration():
    url = "https://www.law.ac.uk/study/undergraduate/law/llb-law-top-up/"
    html = page(heading() + row("London", "£18,100"), title="LLB Law (Top up)")
    html += "<aside><p>Related business course: top up in one year</p></aside>"
    assert parse_course_fees(html, url, today=TODAY)["fee_term"] is None
    html += '<div id="overview"><p>Top up your law diploma in one year.</p></div>'
    assert parse_course_fees(html, url, today=TODAY)["fee_term"] == "Full Course"


@pytest.mark.asyncio
async def test_all_live_population_fee_only_recovery_is_one_fetch_and_fee_only(monkeypatch):
    import httpx
    import app.services.scraper.extractors.ulaw_fees as module
    from collections import Counter

    class FrozenDate(date):
        @classmethod
        def today(cls):
            return TODAY

    monkeypatch.setattr(module, "date", FrozenDate)
    urls = {row["url"]: row["html"] for row in POPULATION.values()}
    fetched = Counter()

    def transport(request):
        url = str(request.url)
        fetched[url] += 1
        return httpx.Response(200, text=urls[url])

    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(
        **kwargs, transport=httpx.MockTransport(transport),
    ))
    states = Counter()
    for course_id, row in POPULATION.items():
        recovered = await module.recover_course_fee_only(row["url"])
        if course_id in UNRESOLVED:
            assert recovered is None
            states["unresolved"] += 1
            continue
        authority = module.validated_fee_variants(recovered["payload"])
        assert authority
        states[authority["status"]] += 1
        assert set(recovered["payload"]) == {
            "course_website", "international_fee", "fee_year", "fee_term", "currency",
            "extraction_method",
        }
    assert states == {"range": 78, "uniform": 4, "unresolved": 3}
    assert len(fetched) == 85 and set(fetched.values()) == {1}