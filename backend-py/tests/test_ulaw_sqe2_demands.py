"""Course-owned SQE2 PDF recovery: no peer English or invented duration."""
import httpx
import pytest
from types import SimpleNamespace

from app.services.scraper.extractors import ulaw_sqe2_demands as demands
from app.services.scraper.duration_review import public_duration_review_status

URL = "https://www.law.ac.uk/study/postgraduate/law/sqe-2-preparation-course/"
PDF = ("https://www.law.ac.uk/globalassets/13.-media--doc-repo/04.-students/"
       "programme-demands/pdf_students_programme-demands-sqe2-preparation-course-ft-pt.pdf")
COURSE_HTML = """<html><main><h1>SQE2 Preparation Course</h1>
<p>Study full-time or part-time. Access materials for one year.</p>
<a href="/study/course-demands/">Course Demands – Find out more</a>
<p>There are six units per week throughout the course (two units per week in the part-time courses).</p></main></html>"""
BLOG_HTML = """<html><h1>SQE preparation courses</h1>
<p>Similarly, the University's full-time SQE2 Preparation Course has 5 weeks of content
but the actual completion time is slightly longer. Time is again left at the end
for a revision course.</p></html>"""
HUB_HTML = f"""<html><h1>Course Demands</h1><a href="{PDF}">
SQE2 Preparation Course (Full-time and Part-time)</a>
<a href="/other-course.pdf">Other course</a></html>"""
PDF_TEXT = """SQE2 Preparation Course
Full Time and Part Time – from September 2025         Course Demands
1. Introduction
This document is produced by The University of Law (the University) to provide
information about the demands of the course for prospective students.
2. Preliminary Knowledge
A sufficient command of English to follow the course to a successful conclusion (an English
Language level equivalent to IELTS 6.5 with a minimum of 6.0 in each component).
"""


def mock_sources(monkeypatch, *, hub=HUB_HTML, pdf_text=PDF_TEXT,
                 blog=BLOG_HTML, blog_status=200):
    original_client = httpx.AsyncClient
    def client(*args, **kwargs):
        return original_client(
            transport=httpx.MockTransport(lambda req: httpx.Response(
                blog_status if str(req.url) == demands.BLOG_URL else 200,
                text=hub if str(req.url) == "https://www.law.ac.uk/study/course-demands/"
                else blog if str(req.url) == demands.BLOG_URL else "wrong",
            )), **kwargs,
        )
    monkeypatch.setattr(demands.httpx, "AsyncClient", client)

    async def pdf(url):
        assert url == PDF
        return pdf_text
    monkeypatch.setattr(demands, "download_pdf_text", pdf)


@pytest.mark.asyncio
async def test_linked_course_owned_pdf_recovery(monkeypatch):
    mock_sources(monkeypatch)
    evidence = await demands.recover_sqe2_english(URL, COURSE_HTML)
    assert {e["field_key"]: e["value"] for e in evidence} == {
        "ielts_overall": 6.5, "ielts_listening": 6.0, "ielts_reading": 6.0,
        "ielts_writing": 6.0, "ielts_speaking": 6.0,
    }
    assert all(e["source_url"] == PDF and "sufficient command of English" in e["snippet"]
               and e["method"] == demands.METHOD for e in evidence)
    assert not any(e["field_key"] == "duration" for e in evidence)


@pytest.mark.asyncio
async def test_missing_or_wrong_owned_document_does_not_borrow_requirements(monkeypatch):
    mock_sources(monkeypatch, pdf_text=PDF_TEXT.replace("SQE2 Preparation Course", "Other Preparation Course"))
    assert await demands.recover_sqe2_english(URL, COURSE_HTML) == []
    assert await demands.recover_sqe2_english(URL, "<h1>SQE2 Preparation Course</h1>") == []
    assert await demands.recover_sqe2_english(
        "https://www.law.ac.uk/study/postgraduate/law/sqe-1-preparation-course/",
        COURSE_HTML,
    ) == []
    assert demands.parse_demands(PDF_TEXT.replace("6.5", "6.0").replace("6.0 in each", "5.5 in each"), PDF)[0]["value"] == 6.0
    assert await demands.recover_sqe2_english(
        URL, '<a href="https://attacker.example/study/course-demands/">Course Demands</a>'
    ) == []


@pytest.mark.asyncio
async def test_course_demands_index_without_matching_pdf_is_not_a_source(monkeypatch):
    mock_sources(monkeypatch, hub='<a href="/other-course.pdf">Other course demands</a>')
    assert await demands.recover_sqe2_english(URL, COURSE_HTML) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("valid_pdf", [True, False])
async def test_pipeline_enforces_owned_scores_and_unknown_duration(monkeypatch, valid_pdf):
    from app.services.scraper.config.context import current_uni_config
    from app.services.scraper.config.loader import load_uni_config
    from app.services.scraper.pipelines.single_course import extract_course

    mock_sources(monkeypatch, pdf_text=PDF_TEXT if valid_pdf else "")
    async def no_ai(*args, **kwargs):
        return {}, 0.0, 0, 0, {"skipped": True}
    monkeypatch.setattr("app.services.scraper.extractors.gemini_primary.extract_primary", no_ai)
    cfg = load_uni_config(slug="law_1902", name="University of Law",
                          scrape_url="https://www.law.ac.uk/study/", create_missing_stub=False)
    token = current_uni_config.set(cfg)
    try:
        out = await extract_course(URL, html=COURSE_HTML, country="United Kingdom",
                                   use_ai_fallback=False)
    finally:
        current_uni_config.reset(token)
    assert not out.get("error"), out.get("error")
    payload = out["payload"]
    assert payload["ielts_overall"] == (6.5 if valid_pdf else None)
    assert all(payload[f"ielts_{part}"] == (6.0 if valid_pdf else None) for part in
               ("listening", "reading", "writing", "speaking"))
    assert payload["duration"] is None and payload["duration_term"] is None
    if valid_pdf:
        assert payload["extraction_method"][demands.REVIEW_KEY]["status"] == "confirmed_unpublished"
        assert payload["extraction_method"][demands.REVIEW_KEY]["sources"][0]["url"] == URL
        assert payload["extraction_method"]["ielts_overall"] == demands.METHOD
        assert {e["source_url"] for e in out["evidence"] if e["field_key"] == "ielts_overall"} == {PDF}
    else:
        assert demands.REVIEW_KEY not in payload.get("extraction_method", {})
        assert not any(e["field_key"] == "ielts_overall" for e in out["evidence"])


def test_duration_review_requires_fetched_course_owned_assertion_and_recovers():
    assert demands.review_sqe2_duration(URL, "") == (None, None)
    assert demands.review_sqe2_duration(URL, "<h1>SQE2 Preparation Course</h1>") == (None, None)
    assert demands.review_sqe2_duration(URL.replace("www.law.ac.uk", "example.org"), COURSE_HTML) == (None, None)
    proof = demands.parse_demands(PDF_TEXT, PDF)
    assert demands.review_sqe2_duration(URL, COURSE_HTML, BLOG_HTML) == (None, None)
    assert demands.review_sqe2_duration(URL, COURSE_HTML, "", proof) == (None, None)
    assert demands.review_sqe2_duration(URL, COURSE_HTML, BLOG_HTML, []) == (None, None)
    status, value = demands.review_sqe2_duration(URL, COURSE_HTML, BLOG_HTML, proof)
    assert value is None and status["status"] == "confirmed_unpublished"
    assert {s["url"] for s in status["sources"]} == {URL, PDF, demands.BLOG_URL}
    row = SimpleNamespace(duration=None, course_website=URL,
                          extraction_method={demands.REVIEW_KEY: status})
    assert public_duration_review_status(row) == status
    row.course_website = "https://example.org/moved"
    assert public_duration_review_status(row) is None
    row.course_website = URL
    row.duration = 2
    assert public_duration_review_status(row) is None
    status, value = demands.review_sqe2_duration(
        URL, COURSE_HTML.replace("</main>", "<p>Course duration: 12 months</p></main>")
    )
    assert status is None and value["duration"] == 12
    assert value["evidence"]["source_url"] == URL
    plain_label = COURSE_HTML.replace("</main>", "<p>Duration: 12 months</p></main>")
    assert demands.review_sqe2_duration(URL, plain_label, BLOG_HTML, proof)[1]["duration"] == 12
    related_only = COURSE_HTML.replace(
        "</main>",
        '<div class="related-courses"><a href="/study/postgraduate/law/other/">'
        "<p>Duration: 4 months</p></a></div></main>",
    )
    status, value = demands.review_sqe2_duration(URL, related_only, BLOG_HTML, proof)
    assert status["status"] == "confirmed_unpublished" and value is None


def test_confirmed_unpublished_is_not_scored_as_present_duration():
    from app.services.scraper.confidence import score_payload
    status, _ = demands.review_sqe2_duration(
        URL, COURSE_HTML, BLOG_HTML, demands.parse_demands(PDF_TEXT, PDF),
    )
    payload = {"duration": None, "ielts_overall": 6.5, "intake_months": ["March"],
               "study_mode": "Full-time", "international_fee": 20000}
    baseline = score_payload(payload)
    reviewed = score_payload({
        **payload, "extraction_method": {demands.REVIEW_KEY: status},
    })
    assert baseline["score"] == reviewed["score"] == 80
    assert baseline["missing"] == reviewed["missing"] == ["duration"]
    assert reviewed["breakdown"]["duration"]["points_earned"] == 0