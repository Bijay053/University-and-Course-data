"""Official admissions excerpts verified 2026-11-02; no live network in tests."""
from unittest.mock import AsyncMock

import pytest

from app.services.scraper.extractors.hsu_linked_english import (
    CENTRAL_URL, COURSES, FIELDS, recover_linked_english,
)
from tests.test_hsu_facts import hsu_database, hsu_postgres  # isolated PostgreSQL fixtures


CENTRAL = """<details><summary>IELTS (Academic)</summary><table>
<tr><td>Undergraduate</td><td>6.0 overall with no less than 5.5 in each component</td></tr>
<tr><td>Postgraduate</td><td>6.0 overall (5.5 each component) <strong>or</strong>
7.0 overall (minimum 6.0 each component), depending on the course</td></tr>
</table></details>"""
UG = "bsc-hons-physiotherapy"
PG = "msc-osteopathy-pre-registration"
REFERRAL = ("International applicants are welcome. Entry requirements and English language "
            "requirements vary by country/qualification. Please refer to our International "
            "Entry Requirements and English Language Requirements guidance.")


def course(slug=UG, text=REFERRAL):
    return (f"<main><h1>{COURSES[slug][0]}</h1><p>Full-time on campus. Duration: 3 years. "
            f"Start date: September.</p><div><h4>International Students</h4><p>{text}</p></div>"
            "<h3>Tuition Fees 2027/28</h3><h4>UK/ROI/Channel Islands</h4>"
            "<p>£10,050 Fees for overseas students are still to be confirmed.</p></main>")


def url(slug=UG):
    return f"https://www.hsu.ac.uk/course/{slug}/"


@pytest.mark.asyncio
@pytest.mark.parametrize("slug", list(COURSES))
async def test_exact_awards_and_selected_central_route(slug):
    text = "Applicants must achieve IELTS level 7.0 or equivalent." if slug == PG else REFERRAL
    payload, evidence = {"ielts_overall": 9, "ielts_writing": 9}, []
    fetch = AsyncMock(return_value=CENTRAL)
    await recover_linked_english(course(slug, text), url(slug), payload, evidence, fetch)
    fetch.assert_awaited_once_with(CENTRAL_URL)
    assert [payload[f] for f in FIELDS] == ([7, 6, 6, 6, 6] if slug == PG else [6, 5.5, 5.5, 5.5, 5.5])
    assert all(e["source_url"] in {CENTRAL_URL, url(slug)} for e in evidence)


@pytest.mark.asyncio
@pytest.mark.parametrize("central", ["", "<p>Challenge</p>", CENTRAL.replace("IELTS (Academic)", "Pearson PTE (Academic)")])
async def test_unavailable_or_wrong_test_guidance_preserves_only_owned_overall(central):
    p, ev = {"ielts_overall": 7, "ielts_reading": 5.5}, []
    await recover_linked_english(course(PG, "IELTS level 7.0."), url(PG), p, ev, AsyncMock(return_value=central))
    assert p["ielts_overall"] == 7
    assert all(p[f] is None for f in FIELDS[1:])


@pytest.mark.asyncio
async def test_timeout_and_pg_alternatives_fail_closed():
    p, ev = {}, []
    fetch = AsyncMock(side_effect=TimeoutError)
    await recover_linked_english(course(), url(), p, ev, fetch)
    assert all(p[f] is None for f in FIELDS)
    fetch.reset_mock()
    await recover_linked_english(course(PG), url(PG), p, ev, fetch)
    fetch.assert_not_awaited()
    assert all(p[f] is None for f in FIELDS)


@pytest.mark.asyncio
@pytest.mark.parametrize("text", [
    "IELTS overall score of 8.0 with no less than 7.5 in each component.",
    "IELTS overall score of 6.5 with no less than 6.0 in each component.",
])
async def test_course_override_never_fetches_central(text):
    p, ev, fetch = {}, [], AsyncMock()
    await recover_linked_english(course(text=text), url(), p, ev, fetch)
    fetch.assert_not_awaited()
    assert p["ielts_overall"] in (8, 6.5)
    assert all(e["source_url"] == url() for e in ev)


@pytest.mark.asyncio
@pytest.mark.parametrize("html,target", [
    (course().replace("BSc (Hons) Physiotherapy", "MSc Physiotherapy"), url()),
    (course(), url("unknown-course")),
    (course(), url("msc-global-healthcare-management")),
    (course(), url("bsc-occupational-therapy-apprenticeship")),
    (course(), "https://not-hsu.ac.uk/course/" + UG + "/"),
    ("<h1>BSc (Hons) Physiotherapy</h1><nav>English language requirements</nav>", url()),
])
async def test_no_cross_award_host_or_navigation_inference(html, target):
    p, ev, fetch = {}, [], AsyncMock()
    await recover_linked_english(html, target, p, ev, fetch)
    assert p == {}
    fetch.assert_not_awaited()


async def pipeline(monkeypatch, slug, text):
    from app.services.scraper.config.context import current_uni_config
    from app.services.scraper.config.loader import load_uni_config
    from app.services.scraper.pipelines.single_course import extract_course
    monkeypatch.setattr("app.services.scraper.extractors.gemini_primary.extract_primary",
                        AsyncMock(return_value=({}, 0.0, 0, 0, {"skipped": True})))
    monkeypatch.setattr("app.services.scraper.pipelines.single_course.fetch_html",
                        AsyncMock(return_value=CENTRAL))
    config = load_uni_config(slug="hsu", name="Health Sciences University",
                             university_id=101, scrape_url="https://www.hsu.ac.uk",
                             create_missing_stub=False)
    config.extraction.skip_per_course_browser = True
    token = current_uni_config.set(config)
    try:
        return await extract_course(url(slug), html=course(slug, text),
                                    country="United Kingdom", use_ai_fallback=False)
    finally:
        current_uni_config.reset(token)


@pytest.mark.asyncio
@pytest.mark.parametrize("slug,text,overall,band", [
    (UG, REFERRAL, 6, 5.5),
    (PG, "Applicants must achieve IELTS level 7.0 or equivalent.", 7, 6),
    (UG, "IELTS overall score of 8.0 with no less than 7.5 in each component.", 8, 7.5),
])
async def test_pipeline_and_persisted_review(hsu_database, monkeypatch, slug, text, overall, band):
    from uuid import uuid4
    from app.models import University
    from app.models.scrape_runtime import ScrapeRuntimeJob
    from app.routers.scrape import staged_review
    from app.services.scraper.stage_course import stage_course
    from app.services.scraper.config.context import current_uni_config
    from app.services.scraper.config.loader import load_uni_config

    result = await pipeline(monkeypatch, slug, text)
    assert [result["payload"][f] for f in FIELDS] == [overall, band, band, band, band]
    # No domestic/unpublished tuition is promoted while recovering English.
    assert result["payload"]["international_fee"] is None
    selected = [e for e in result["evidence"] if e.get("decision_status") == "selected"]
    for field in FIELDS:
        matches = [e for e in selected if e["field_key"] == field]
        assert len(matches) == 1
        expected = url(slug) if overall == 8 or (slug == PG and field == "ielts_overall") else CENTRAL_URL
        assert matches[0]["source_url"] == expected
    async with hsu_database() as db:
        uni = University(name="HSU linked English isolated", country="United Kingdom", city="Bournemouth")
        db.add(uni)
        await db.flush()
        job = f"hsu-english-{uuid4()}"
        db.add(ScrapeRuntimeJob(runtime_job_id=job, university_id=uni.id,
                               job_type="scrape", status="completed"))
        await db.flush()
        config = load_uni_config(slug="hsu", name="Health Sciences University",
                                 university_id=101, scrape_url="https://www.hsu.ac.uk",
                                 create_missing_stub=False)
        token = current_uni_config.set(config)
        try:
            staged = await stage_course(db, scrape_job_id=job, university_id=uni.id,
                                        course_name=COURSES[slug][0], source_url=url(slug),
                                        payload=result["payload"], evidence=result["evidence"])
        finally:
            current_uni_config.reset(token)
        assert staged.saved, staged.reason
        row_id = staged.scraped_course_id
        await db.commit()
    async with hsu_database() as db:
        review = await staged_review(row_id, db)
        assert review["course"]["internationalFee"] is None
        for field in FIELDS:
            winners = [e for e in review["evidence"] if e["field_key"] == field and e["selected"]]
            assert len(winners) == 1, field
            assert float(winners[0]["candidate_value"]) == (overall if field == "ielts_overall" else band)
            assert winners[0]["source_url"] == (
                url(slug) if overall == 8 or (slug == PG and field == "ielts_overall") else CENTRAL_URL)
            assert "IELTS" in winners[0]["snippet"]
        for field, camel in zip(FIELDS, ("ieltsOverall", "ieltsReading", "ieltsWriting", "ieltsListening", "ieltsSpeaking")):
            assert float(review["course"][camel]) == (overall if field == "ielts_overall" else band)
