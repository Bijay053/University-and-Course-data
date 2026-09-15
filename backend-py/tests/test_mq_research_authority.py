"""Focused regressions for MQ's authoritative PhD/MPhil supplement."""
from __future__ import annotations

import json

import pytest
from sqlalchemy import func, select

from app.services.scraper import mq_browser_discover as mq


def _page_data_body(program: dict) -> str:
    return json.dumps({
        "result": {
            "data": {
                "current": {
                    "fields": {"json": json.dumps(program)},
                },
            },
        },
    })


def _funnelback_rows(count: int) -> list[dict]:
    return [
        {
            "title": f"Bachelor of Coverage Test {index}",
            "liveUrl": (
                "https://www.mq.edu.au/study/find-a-course/courses/"
                f"coverage-course-{index}"
            ),
            "metaData": {},
        }
        for index in range(count)
    ]


def test_research_authority_urls_are_the_committed_public_sources():
    assert mq._MQ_RESEARCH_INDEX_URL == (
        "https://www.mq.edu.au/research/phd-and-research-degrees/"
        "explore-research-degrees"
    )
    assert mq._MQ_RESEARCH_FEES_URL == (
        "https://www.mq.edu.au/study/admissions-and-entry/fees-and-costs/"
        "research-students"
    )
    assert {
        item["title"] for item in mq._MQ_RESEARCH_AUTHORITIES
    } == {"Doctor of Philosophy", "Master of Philosophy"}


@pytest.mark.parametrize(
    ("title", "duration_text"),
    [
        ("Doctor of Philosophy", "Three years full-time equivalent"),
        ("Master of Philosophy", "Two years full-time equivalent"),
    ],
)
def test_authority_requires_exact_title_full_time_and_international_evidence(
    title, duration_text,
):
    authority = next(
        item for item in mq._MQ_RESEARCH_AUTHORITIES if item["title"] == title
    )
    valid = (
        f"<title>{title} | Research degrees | Macquarie University</title>"
        f"<p>Duration: {duration_text}</p>"
        "<p>Scholarships may include OSHC and visa application costs for "
        "international students.</p>"
    )
    evidence = mq._extract_research_authority_evidence(valid, authority)
    assert evidence is not None
    assert "full-time" in evidence["duration_text"].lower()

    assert mq._extract_research_authority_evidence(
        valid.replace("international students", "students"),
        authority,
    ) is None
    assert mq._extract_research_authority_evidence(
        valid.replace("full-time", "part-time"),
        authority,
    ) is None


def test_authority_result_never_copies_a_domestic_fee():
    authority = next(
        item for item in mq._MQ_RESEARCH_AUTHORITIES
        if item["title"] == "Doctor of Philosophy"
    )
    evidence = {
        "source_url": authority["url"],
        "duration_text": "Three years full-time equivalent",
        "full_time_text": "full-time equivalent",
        "international_text": "international students",
    }
    result = mq._build_research_authority_result(authority, evidence, {
        "fees": [{
            "fee_type": {"label": "Domestic Fee-paying"},
            "estimated_annual_fee": "11700",
        }],
    })
    payload = result["payload"]
    assert "international_fee" not in payload
    assert payload["has_central_fee_page"] is False
    assert payload["duration"] == 3.0
    assert payload["study_load"] == "Full Time"
    assert payload["international_full_time_source_verified"] is True
    assert any(
        row["method"] == "mq:research_fee_authority"
        for row in result["evidence"]
    )

    verified = mq._build_research_authority_result(
        authority,
        evidence,
        {},
        {
            "source_url": mq._MQ_RESEARCH_FEES_URL,
            "snippet": "international students must pay international fees",
        },
    )
    assert verified["payload"]["has_central_fee_page"] is True
    assert all(
        row["method"] != "funnelback:title"
        for row in verified["evidence"]
    )
    mphil = next(
        item for item in mq._MQ_RESEARCH_AUTHORITIES
        if item["title"] == "Master of Philosophy"
    )
    mphil_result = mq._build_research_authority_result(
        mphil,
        {
            "source_url": mphil["url"],
            "duration_text": mphil["duration_text"],
            "full_time_text": "full-time equivalent",
            "international_text": "international students",
        },
        {},
        {
            "source_url": mq._MQ_RESEARCH_FEES_URL,
            "snippet": "international students must pay international fees",
        },
    )
    assert mphil_result["payload"]["degree_level"] == "Master's"
    assert mphil_result["payload"]["academic_level"] == "Postgraduate"


def test_research_route_requires_exact_qualification_identity():
    mphil = next(
        item for item in mq._MQ_RESEARCH_AUTHORITIES
        if item["title"] == "Master of Philosophy"
    )
    body = (
        '<a href="/study/find-a-course/research/doctor-of-philosophy">'
        "unrelated first result</a>"
        '<a href="/study/find-a-course/research/2026/master-of-philosophy">'
        "exact result</a>"
    )
    assert mq._research_admissions_route(body, mphil).endswith(
        "/study/find-a-course/research/master-of-philosophy"
    )
    assert mq._research_admissions_route(
        '<a href="/study/find-a-course/research/doctor-of-philosophy">'
        "only unrelated result</a>",
        mphil,
    ).endswith("/study/find-a-course/research/master-of-philosophy")

    malformed = {
        **mphil,
        "admissions_url": (
            "https://www.mq.edu.au/study/find-a-course/research/"
            "doctor-of-philosophy"
        ),
    }
    assert mq._research_admissions_route("", malformed) is None


@pytest.mark.asyncio
async def test_authority_supplement_uses_current_combined_route_and_page_data(
    monkeypatch,
):
    authority = {
        "title": "Master of Philosophy",
        "url": (
            "https://www.mq.edu.au/research/phd-and-research-degrees/"
            "explore-research-degrees/master-of-philosophy"
        ),
        "duration": 2.0,
        "duration_text": "Two years full-time equivalent",
        # A year-stamped route is deliberately advertised in this fixture.
        "admissions_url": (
            "https://www.mq.edu.au/study/find-a-course/research/2026/"
            "master-of-philosophy"
        ),
    }
    monkeypatch.setattr(mq, "_MQ_RESEARCH_AUTHORITIES", (authority,))
    source_body = (
        f"<h1>{authority['title']}</h1>"
        "<p>Duration: Two years full-time equivalent</p>"
        "<p>Time commitment: 35 hours per week (full time).</p>"
        "<p>Scholarships may include OSHC for international students.</p>"
        '<a href="/study/find-a-course/research/2026/master-of-philosophy">'
        "View course</a>"
    )
    calls: list[str] = []

    async def fetch(url):
        calls.append(url)
        if url.endswith("/master-of-philosophy"):
            return source_body
        if url == mq._MQ_RESEARCH_FEES_URL:
            return (
                "<p>If you are studying on an international student visa, "
                "you must pay international fees.</p>"
            )
        # Current-route page-data is intentionally complete and contains an
        # explicit international amount.  The authority duration must still
        # win over any conflicting generic duration in this payload.
        return _page_data_body({
            "course_name": "Master of Philosophy",
            "course_duration_in_years": {"label": "Part time: 4 years"},
            "fees": [
                {
                    "fee_type": {"label": "Domestic Fee-paying"},
                    "estimated_annual_fee": "11700",
                },
                {
                    "fee_type": {"label": "International Fee-paying"},
                    "estimated_annual_fee": "30000",
                },
            ],
        })

    monkeypatch.setattr(mq, "_fetch_mq_research_source", fetch)
    emits: list[str] = []

    async def emit(message):
        emits.append(message)

    links = await mq._discover_from_research_authority(
        set(), emit, max_courses=10,
    )
    assert len(links) == 1
    link = links[0]
    assert link["url"].endswith(
        "/study/find-a-course/research/master-of-philosophy"
    )
    payload = link["scrapy_result"]["payload"]
    assert payload["duration"] == 2.0
    assert payload["degree_level"] == "Master's"
    assert payload["academic_level"] == "Postgraduate"
    assert payload["study_load"] == "Full Time"
    assert payload["international_fee"] == 30000.0
    assert all("/2026/" not in url for url in calls if "find-a-course" in url)
    assert any("authoritative research course verified" in message for message in emits)


@pytest.mark.asyncio
async def test_authority_source_requests_one_bounded_render_retry(monkeypatch):
    """The provider gets one retry for a transient authority transport failure."""
    import app.services.scraper.http_fetcher as http_fetcher

    authority = next(
        item for item in mq._MQ_RESEARCH_AUTHORITIES
        if item["title"] == "Master of Philosophy"
    )
    valid_body = (
        "<title>Master of Philosophy | Research degrees | Macquarie University"
        "</title><p>Two years full-time equivalent</p>"
        "<p>International students may apply.</p>"
    )
    calls: list[dict] = []

    async def provider_retry_success(url, **kwargs):
        calls.append(kwargs)
        return valid_body

    monkeypatch.setattr(
        http_fetcher,
        "fetch_html_scrape_do",
        provider_retry_success,
    )

    class _FallbackClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, *args, **kwargs):
            raise AssertionError("direct fallback should not run before retry")

    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", _FallbackClient)

    body = await mq._fetch_mq_research_source(str(authority["url"]))

    assert body == valid_body
    assert len(calls) == 1
    assert calls[0]["max_retries"] == 1


@pytest.mark.asyncio
async def test_unrelated_page_data_title_cannot_supply_research_fee(
    monkeypatch,
):
    authority = next(
        item for item in mq._MQ_RESEARCH_AUTHORITIES
        if item["title"] == "Doctor of Philosophy"
    )
    source_body = (
        "<title>Doctor of Philosophy | Research degrees | Macquarie University"
        "</title><p>Three years full-time equivalent</p>"
        "<p>International students may apply.</p>"
    )
    unrelated_page_data = _page_data_body({
        "course_name": "Bachelor of Unrelated Studies",
        "fees": [{
            "fee_type": {"label": "International"},
            "estimated_annual_fee": "99999",
        }],
    })
    monkeypatch.setattr(mq, "_MQ_RESEARCH_AUTHORITIES", (authority,))

    async def fetch(url):
        if url == authority["url"]:
            return source_body
        if url == mq._MQ_RESEARCH_FEES_URL:
            return "<p>International students must pay international fees.</p>"
        return unrelated_page_data

    monkeypatch.setattr(mq, "_fetch_mq_research_source", fetch)
    emits: list[str] = []

    async def emit(message):
        emits.append(message)

    links = await mq._discover_from_research_authority(
        set(), emit, max_courses=10,
    )
    assert len(links) == 1
    payload = links[0]["scrapy_result"]["payload"]
    assert payload["course_name"] == "Doctor of Philosophy"
    assert payload["duration"] == 3.0
    assert payload.get("international_fee") is None
    assert any("page-data title identity mismatch" in message for message in emits)


@pytest.mark.asyncio
async def test_both_authority_scrapy_results_extract_and_stage_with_evidence():
    """Exercise the real provider short-circuit and staging boundary together."""
    from app.database import AsyncSessionLocal
    from app.models import ScrapedCourse, ScrapedFieldEvidence, University
    from app.services.scraper.config.context import set_uni_config
    from app.services.scraper.config.loader import load_uni_config
    from app.services.scraper.orchestrator import _extract_only
    from app.services.scraper.stage_course import stage_course

    set_uni_config(load_uni_config(
        slug="mq",
        name="Macquarie University",
        scrape_url="https://www.mq.edu.au/study/find-a-course",
    ))
    fee_source_evidence = {
        "source_url": mq._MQ_RESEARCH_FEES_URL,
        "snippet": "international students must pay international fees",
    }
    staged_ids: list[int] = []
    async with AsyncSessionLocal() as db:
        uni_id = (
            await db.execute(
                select(University.id)
                .where(func.lower(University.name).like("%macquarie%"))
                .limit(1)
            )
        ).scalar_one_or_none()
        if uni_id is None:
            pytest.skip("local staging database has no Macquarie University row")

    try:
        for authority in mq._MQ_RESEARCH_AUTHORITIES:
            source_evidence = {
                "source_url": authority["url"],
                "duration_text": authority["duration_text"],
                "full_time_text": "full-time equivalent",
                "international_text": "international students",
            }
            built = mq._build_research_authority_result(
                authority,
                source_evidence,
                {},
                fee_source_evidence,
            )
            link = {
                "name": authority["title"],
                "url": built["url"],
                "scrapy_result": built,
            }
            extracted = await _extract_only(link, country=None)
            assert extracted is built
            payload = extracted["payload"]
            assert payload["course_name"] == authority["title"]
            assert payload["duration"] == authority["duration"]
            assert payload["study_load"] == "Full Time"
            assert payload["international_full_time_source_verified"] is True
            assert payload.get("international_fee") is None

            job_id = f"task467_authority_{authority['title'][:3].lower()}"
            async with AsyncSessionLocal() as db:
                staged = await stage_course(
                    db,
                    scrape_job_id=job_id,
                    university_id=uni_id,
                    course_name=authority["title"],
                    payload=payload,
                    evidence=extracted["evidence"],
                    source_url=extracted["url"],
                )
                assert staged.saved, staged.reason
                staged_ids.append(staged.scraped_course_id)
                row = await db.get(ScrapedCourse, staged.scraped_course_id)
                assert row is not None
                assert row.international_fee is None
                assert row.fee_term is None
                assert row.duration == authority["duration"]
                expected_degree = (
                    "Doctorate"
                    if authority["title"] == "Doctor of Philosophy"
                    else "Master's"
                )
                assert row.degree_level == expected_degree
                assert row.study_load == "Full Time"
                evidence_rows = (
                    await db.execute(
                        select(ScrapedFieldEvidence).where(
                            ScrapedFieldEvidence.scraped_course_id == row.id,
                        )
                    )
                ).scalars().all()
                methods = {item.extraction_method for item in evidence_rows}
                assert "mq:research_authority_full_time" in methods
                assert "mq:research_authority_international_evidence" in methods
                assert "mq:research_fee_authority" in methods
    finally:
        if staged_ids:
            async with AsyncSessionLocal() as db:
                for staged_id in staged_ids:
                    row = await db.get(ScrapedCourse, staged_id)
                    if row is not None:
                        await db.delete(row)
                await db.commit()


def test_current_route_canonicalisation_covers_research_and_combined_paths():
    for level, slug in (
        ("research", "doctor-of-philosophy"),
        ("undergraduate", "bachelor-of-laws-master-of-laws"),
        ("courses", "bachelor-of-arts"),
    ):
        current = mq._canonical_mq_admissions_url(
            f"https://www.mq.edu.au/study/find-a-course/{level}/2026/{slug}"
        )
        assert current.endswith(f"/{level}/{slug}")
        assert "/2026/" not in current


@pytest.mark.asyncio
async def test_page_data_guard_uses_candidate_denominator_before_supplement(
    monkeypatch,
):
    """Three of five page-data rows must fail 80%, even if supplement wins."""
    import httpx
    import app.services.scraper.http_fetcher as http_fetcher

    rows = _funnelback_rows(5)
    funnelback_body = json.dumps({
        "response": {"resultPacket": {"results": rows}},
    })
    complete_body = _page_data_body({
        "course_name": "Bachelor of Coverage Test",
        "fees": [{
            "fee_type": {"label": "International"},
            "estimated_annual_fee": "40000",
        }],
    })
    failure = {"kind": "challenge_page"}

    async def fake_scrape_do(url, **kwargs):
        if "s/search.json" in url:
            return funnelback_body
        slug = url.split("/courses/")[-1].split("/page-data")[0]
        if slug in {"coverage-course-3", "coverage-course-4"}:
            return None
        return complete_body

    class FakeResponse:
        status_code = 403
        text = ""

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, *args, **kwargs):
            return FakeResponse()

    supplement_calls: list[int] = []

    async def successful_supplement(*args, **kwargs):
        supplement_calls.append(1)
        return [{"name": "Doctor of Philosophy", "url": "https://mq.test/phd"}]

    async def emit(*args, **kwargs):
        return None

    monkeypatch.setattr(http_fetcher, "fetch_html_scrape_do", fake_scrape_do)
    monkeypatch.setattr(
        http_fetcher, "get_last_fetch_failure", lambda: failure,
    )
    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(mq, "_FUNNELBACK_MIN_RESULTS", 1)
    monkeypatch.setattr(mq, "_RICH_COURSE_MIN_RESULTS", 1)
    monkeypatch.setattr(mq, "_MQ_RESEARCH_AUTHORITY_MIN_CATALOGUE", 1)
    monkeypatch.setattr(
        mq, "_discover_from_research_authority", successful_supplement,
    )

    with pytest.raises(
        mq.MqEnrichmentCoverageError,
        match=r"page-data coverage.*3/5.*80\.0%",
    ):
        await mq._discover_from_funnelback_api(emit, max_courses=100)
    assert supplement_calls == []


@pytest.mark.asyncio
async def test_fee_guard_uses_candidate_denominator_before_supplement(
    monkeypatch,
):
    """Three of five fee rows must fail 70%, even if supplement wins."""
    import httpx
    import app.services.scraper.http_fetcher as http_fetcher

    rows = _funnelback_rows(5)
    funnelback_body = json.dumps({
        "response": {"resultPacket": {"results": rows}},
    })
    fee_programs = [
        {
            "course_name": f"Bachelor of Coverage Test {index}",
            "fees": (
                [{
                    "fee_type": {"label": "International"},
                    "estimated_annual_fee": "40000",
                }]
                if index < 3 else []
            ),
        }
        for index in range(5)
    ]

    async def fake_scrape_do(url, **kwargs):
        if "s/search.json" in url:
            return funnelback_body
        index = int(url.split("coverage-course-")[-1].split("/")[0])
        return _page_data_body(fee_programs[index])

    class FakeResponse:
        status_code = 403
        text = ""

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, *args, **kwargs):
            return FakeResponse()

    supplement_calls: list[int] = []

    async def successful_supplement(*args, **kwargs):
        supplement_calls.append(1)
        return [{"name": "Master of Philosophy", "url": "https://mq.test/mphil"}]

    async def emit(*args, **kwargs):
        return None

    monkeypatch.setattr(http_fetcher, "fetch_html_scrape_do", fake_scrape_do)
    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(mq, "_FUNNELBACK_MIN_RESULTS", 1)
    monkeypatch.setattr(mq, "_RICH_COURSE_MIN_RESULTS", 1)
    monkeypatch.setattr(mq, "_MQ_RESEARCH_AUTHORITY_MIN_CATALOGUE", 1)
    monkeypatch.setattr(
        mq, "_discover_from_research_authority", successful_supplement,
    )

    with pytest.raises(
        mq.MqEnrichmentCoverageError,
        match=r"international-fee coverage.*3/5.*70\.0%",
    ):
        await mq._discover_from_funnelback_api(emit, max_courses=100)
    assert supplement_calls == []


def test_research_supplement_does_not_change_rich_provider_guards():
    assert mq._PAGE_DATA_MIN_COVERAGE == 0.80
    assert mq._INTERNATIONAL_FEE_MIN_COVERAGE == 0.70
