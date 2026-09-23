import pytest
import asyncio
from fastapi import HTTPException
from types import SimpleNamespace
import uuid
from sqlalchemy import delete, select

from app.database import AsyncSessionLocal
from app.models import ScrapedCourse, ScrapeRuntimeJob, University
from app.routers.scrape import (
    DatedCatalogueAuditBody,
    DatedCatalogueDecisionBody,
    _dated_source_fingerprint,
    audit_dated_catalogue_reviews,
    decide_dated_catalogue_review,
    history_one,
    read_dated_catalogue_reviews,
    staged_update,
)
from app.services import winchester_catalogue_review as review


def _source(title: str, awards: list[str], *, verified: bool = True) -> dict:
    return {
        "url": "https://www.winchester.ac.uk/study/course/",
        "verified": verified,
        "status": 200,
        "title": title if verified else None,
        "awards": awards if verified else [],
        "reason": None if verified else "challenge",
    }


def test_year_stripping_proposes_reference_url_without_changing_archive_identity():
    original = (
        "https://www.winchester.ac.uk/study/research-degrees/"
        "Courses/2025/MPhilPhD-Research-2025/"
    )
    assert review.candidate_url(original) == (
        "https://www.winchester.ac.uk/study/research-degrees/"
        "Courses/MPhilPhD-Research/"
    )
    assert "/2025/" in original


@pytest.mark.parametrize(
    ("archived", "current"),
    [
        (_source("BA (Hons) History", ["BA (HONS)"]), _source("MA History", ["MA"])),
        (_source("MA Education", ["MA"]), _source("MPhil/PhD Education", ["MPhil/PhD"])),
        (_source("MPhil/PhD Research", ["MPhil/PhD"]), _source("PhD Research", ["PHD"])),
    ],
)
def test_different_awards_are_never_suggested_as_counterparts(archived, current):
    suggestion, reason = review._comparison(archived, current)
    assert suggestion == "not_counterpart"
    assert "different awards" in reason


def test_foundation_and_top_up_variants_are_kept_distinct():
    suggestion, reason = review._comparison(
        _source("BA (Hons) Business Foundation", ["BA (HONS)"]),
        _source("BA (Hons) Business", ["BA (HONS)"]),
    )
    assert suggestion == "not_counterpart"
    assert "Foundation or top-up" in reason


def test_unverified_source_never_becomes_false_counterpart_proof():
    suggestion, reason = review._comparison(
        _source("", [], verified=False),
        _source("MA Politics", ["MA"]),
    )
    assert suggestion is None
    assert "could not be verified" in reason


def test_matching_titles_without_recognized_awards_are_not_counterpart_proof():
    suggestion, reason = review._comparison(
        _source("Creative Writing", []),
        _source("Creative Writing", []),
    )
    assert suggestion is None
    assert "without recognized awards" in reason


def test_explicit_year_directory_archive_is_not_asserted_as_current_counterpart():
    archived = _source("MPhil/PhD Research 2025", ["MPhil/PhD"])
    archived["url"] = (
        "https://www.winchester.ac.uk/study/research-degrees/"
        "Courses/2025/MPhilPhD-Research-2025/"
    )
    suggestion, reason = review._comparison(
        archived,
        _source("MPhil/PhD Research", ["MPhil/PhD"]),
    )
    assert suggestion is None
    assert "explicit year-directory archive" in reason


@pytest.mark.asyncio
async def test_live_html_challenge_is_reported_unverified(monkeypatch):
    class Response:
        status_code = 200
        text = "<html><title>Just a moment...</title><form id='challenge-form'></form></html>"
        headers = {}

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, _url):
            return Response()

    monkeypatch.setattr(review.httpx, "AsyncClient", lambda **_kwargs: Client())

    async def public(_host):
        return None

    monkeypatch.setattr(review, "_assert_public_host", public)
    result = await review._fetch_official(
        "https://www.winchester.ac.uk/study/Postgraduate/Courses/MA-Politics-2025/"
    )
    assert result["verified"] is False
    assert "challenge" in result["reason"].lower()


@pytest.mark.asyncio
async def test_decision_is_durable_scoped_and_optimistically_concurrent():
    source_url = "https://www.winchester.ac.uk/study/Postgraduate/Courses/MA-Politics-2025/"
    row = SimpleNamespace(
        id=9,
        scrape_job_id="job-exact",
        university_id=87,
        course_name="MA Politics 2025",
        course_website=source_url,
        scrape_warnings=["dated_catalogue_page_review"],
        extraction_method={
            "course_name": {"method": "title"},
            "dated_catalogue_review": {
                "revision": 2,
                "evidence": {"checkedAt": "2026-07-20T00:00:00Z"},
                "sourceUrl": source_url,
                "sourceFingerprint": _dated_source_fingerprint(source_url),
                "decision": None,
                "history": [{"type": "official_source_audit", "at": "2026-07-20T00:00:00Z"}],
            },
        },
    )

    class Result:
        def scalar_one_or_none(self):
            return row

    class DB:
        commits = 0

        async def execute(self, _statement):
            return Result()

        async def commit(self):
            self.commits += 1

        async def rollback(self):
            raise AssertionError("rollback not expected")

    db = DB()
    response = await decide_dated_catalogue_review(
        9,
        DatedCatalogueDecisionBody(
            universityId=87,
            jobId="job-exact",
            expectedRevision=2,
            decision="keep_separate",
        ),
        db,
        {"id": 4, "email": "reviewer@example.test", "name": "Reviewer"},
    )
    assert response["review"]["decision"] == "keep_separate"
    assert response["review"]["revision"] == 3
    assert row.course_website.endswith("MA-Politics-2025/")
    assert row.scrape_warnings == ["dated_catalogue_page_review"]
    assert row.extraction_method["course_name"] == {"method": "title"}
    assert [item["type"] for item in response["review"]["history"]] == [
        "official_source_audit",
        "reviewer_decision",
    ]
    assert db.commits == 1

    with pytest.raises(HTTPException) as stale:
        await decide_dated_catalogue_review(
            9,
            DatedCatalogueDecisionBody(
                universityId=87,
                jobId="job-exact",
                expectedRevision=2,
                decision="current_counterpart",
            ),
            db,
            {"id": 5, "email": "other@example.test", "name": "Other"},
        )
    assert stale.value.status_code == 409


@pytest.mark.asyncio
async def test_current_counterpart_decision_requires_verified_backend_suggestion():
    source_url = "https://www.winchester.ac.uk/study/Postgraduate/Courses/MA-History-2025/"
    row = SimpleNamespace(
        id=10,
        scrape_job_id="job-exact",
        university_id=87,
        course_name="MA History",
        course_website=source_url,
        scrape_warnings=["dated_catalogue_page_review"],
        extraction_method={
            "dated_catalogue_review": {
                "revision": 1,
                "evidence": {
                    "checkedAt": "2026-07-20T00:00:00Z",
                    "suggestion": "current_counterpart",
                    "original": {
                        "url": source_url,
                        "verified": True,
                        "title": "Creative Writing",
                        "awards": [],
                    },
                    "candidate": {
                        "url": source_url.replace("-2025", ""),
                        "verified": True,
                        "title": "Creative Writing",
                        "awards": [],
                    },
                },
                "sourceUrl": source_url,
                "sourceFingerprint": _dated_source_fingerprint(source_url),
            },
        },
    )

    class Result:
        def scalar_one_or_none(self):
            return row

    class DB:
        async def execute(self, _statement):
            return Result()

    with pytest.raises(HTTPException) as denied:
        await decide_dated_catalogue_review(
            10,
            DatedCatalogueDecisionBody(
                universityId=87,
                jobId="job-exact",
                expectedRevision=1,
                decision="current_counterpart",
            ),
            DB(),
            {"id": 4, "email": "reviewer@example.test", "name": "Reviewer"},
        )
    assert denied.value.status_code == 422


@pytest.mark.asyncio
async def test_archive_redirect_keeps_requested_identity_and_cannot_authorize_decision(monkeypatch):
    source_url = (
        "https://www.winchester.ac.uk/study/Postgraduate/"
        "Courses/2025/MA-Archive-2025/"
    )
    final_url = "https://www.winchester.ac.uk/study/Postgraduate/Courses/MA-Archive/"

    def handler(request):
        if str(request.url) == source_url:
            return review.httpx.Response(302, headers={"location": final_url})
        return review.httpx.Response(
            200,
            text="<html><title>MA Archive - University of Winchester</title></html>",
        )

    transport = review.httpx.MockTransport(handler)
    real_client = review.httpx.AsyncClient
    monkeypatch.setattr(
        review.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=transport, **kwargs),
    )

    async def public(_host):
        return None

    monkeypatch.setattr(review, "_assert_public_host", public)
    row = SimpleNamespace(
        id=11,
        scrape_job_id="job-archive-redirect",
        university_id=87,
        course_name="MA Archive",
        course_website=source_url,
        scrape_warnings=["dated_catalogue_page_review"],
        extraction_method={},
    )

    async def exact_rows(_db, _body, *, lock=False):
        return [row]

    class Result:
        def scalar_one_or_none(self):
            return row

    class DB:
        async def execute(self, _statement):
            return Result()

        async def commit(self):
            return None

        async def rollback(self):
            raise AssertionError("rollback not expected")

    monkeypatch.setattr("app.routers.scrape._exact_dated_rows", exact_rows)
    body = DatedCatalogueAuditBody(
        universityId=87,
        rows=[{"id": 11, "jobId": "job-archive-redirect"}],
    )
    audited = await audit_dated_catalogue_reviews(body, DB(), {})
    evidence = audited["rows"][0]["review"]["evidence"]
    assert evidence["original"]["url"] == source_url
    assert evidence["original"]["finalUrl"] == final_url
    assert evidence["suggestion"] is None

    with pytest.raises(HTTPException) as denied:
        await decide_dated_catalogue_review(
            11,
            DatedCatalogueDecisionBody(
                universityId=87,
                jobId="job-archive-redirect",
                expectedRevision=1,
                decision="current_counterpart",
            ),
            DB(),
            {"id": 4, "email": "reviewer@example.test", "name": "Reviewer"},
        )
    assert denied.value.status_code == 422


@pytest.mark.asyncio
async def test_reaudits_append_evidence_history_instead_of_overwriting(monkeypatch):
    row = SimpleNamespace(
        id=12,
        scrape_job_id="job-audit-history",
        university_id=87,
        course_name="MA Audit",
        course_website="https://www.winchester.ac.uk/study/Postgraduate/Courses/MA-Audit-2025/",
        scrape_warnings=[],
        extraction_method={"dated_catalogue_review": {"revision": 0, "history": []}},
    )

    async def exact_rows(_db, _body, *, lock=False):
        return [row]

    audit_number = 0

    async def official_audit(_url):
        nonlocal audit_number
        audit_number += 1
        return {
            "checkedAt": f"2026-07-2{audit_number}T00:00:00Z",
            "original": {"verified": True},
            "candidate": {"verified": True},
            "suggestion": "current_counterpart",
            "reason": "verified",
            "referenceOnly": True,
        }

    class DB:
        async def commit(self):
            return None

        async def rollback(self):
            raise AssertionError("rollback not expected")

    monkeypatch.setattr("app.routers.scrape._exact_dated_rows", exact_rows)
    monkeypatch.setattr(
        "app.services.winchester_catalogue_review.audit_dated_route",
        official_audit,
    )
    body = DatedCatalogueAuditBody(
        universityId=87,
        rows=[{"id": 12, "jobId": "job-audit-history"}],
    )
    await audit_dated_catalogue_reviews(body, DB(), {})
    await audit_dated_catalogue_reviews(body, DB(), {})
    stored = row.extraction_method["dated_catalogue_review"]
    assert stored["revision"] == 2
    assert [event["at"] for event in stored["history"]] == [
        "2026-07-21T00:00:00Z",
        "2026-07-22T00:00:00Z",
    ]
    assert row.scrape_warnings == ["dated_catalogue_page_review"]


@pytest.mark.asyncio
async def test_saved_decision_reloads_from_real_database_session():
    job_id = f"test_dated_review_{uuid.uuid4().hex}"
    source_url = "https://www.winchester.ac.uk/study/Postgraduate/Courses/MA-Durable-Review-2025/"
    async with AsyncSessionLocal() as db:
        university_id = (await db.execute(select(University.id).limit(1))).scalar_one_or_none()
        if university_id is None:
            pytest.skip("requires a university row")
        staged = ScrapedCourse(
            scrape_job_id=job_id,
            university_id=university_id,
            course_name="MA Durable Review",
            course_website=source_url,
            scrape_warnings=["dated_catalogue_page_review"],
            extraction_method={
                "dated_catalogue_review": {
                    "revision": 1,
                    "evidence": {
                        "checkedAt": "2026-07-20T00:00:00Z",
                        "suggestion": None,
                    },
                    "sourceUrl": source_url,
                    "sourceFingerprint": _dated_source_fingerprint(source_url),
                    "history": [{
                        "type": "official_source_audit",
                        "at": "2026-07-20T00:00:00Z",
                    }],
                },
            },
        )
        db.add(staged)
        await db.commit()
        await db.refresh(staged)
        staged_id = staged.id

    try:
        async with AsyncSessionLocal() as db:
            await decide_dated_catalogue_review(
                staged_id,
                DatedCatalogueDecisionBody(
                    universityId=university_id,
                    jobId=job_id,
                    expectedRevision=1,
                    decision="not_counterpart",
                ),
                db,
                {"id": 8, "email": "durable@example.test", "name": "Durable Reviewer"},
            )
        async with AsyncSessionLocal() as db:
            reloaded = await db.get(ScrapedCourse, staged_id)
            review_payload = reloaded.extraction_method["dated_catalogue_review"]
            assert review_payload["decision"] == "not_counterpart"
            assert review_payload["revision"] == 2
            assert [entry["type"] for entry in review_payload["history"]] == [
                "official_source_audit",
                "reviewer_decision",
            ]
    finally:
        async with AsyncSessionLocal() as db:
            await db.execute(delete(ScrapedCourse).where(ScrapedCourse.scrape_job_id == job_id))
            await db.commit()


@pytest.mark.asyncio
async def test_history_api_returns_real_dated_review_identity_contract():
    job_id = f"test_dated_history_{uuid.uuid4().hex}"
    async with AsyncSessionLocal() as db:
        university_id = (await db.execute(select(University.id).limit(1))).scalar_one()
        db.add(ScrapeRuntimeJob(
            runtime_job_id=job_id,
            university_id=university_id,
            university_name="University of Winchester",
            url="https://www.winchester.ac.uk/study/",
            job_type="scrape",
            status="completed",
        ))
        db.add(ScrapedCourse(
            scrape_job_id=job_id,
            university_id=university_id,
            course_name="MA Historical",
            course_website="https://www.winchester.ac.uk/study/Postgraduate/Courses/MA-Historical-2025/",
            scrape_warnings=["dated_catalogue_page_review"],
        ))
        await db.commit()
    try:
        async with AsyncSessionLocal() as db:
            payload = await history_one(job_id, db)
        historical = payload["stagedCourses"][0]
        assert historical["scrapeJobId"] == job_id
        assert historical["universityId"] == university_id
        assert historical["scrapeWarnings"] == ["dated_catalogue_page_review"]
    finally:
        async with AsyncSessionLocal() as db:
            await db.execute(delete(ScrapeRuntimeJob).where(ScrapeRuntimeJob.runtime_job_id == job_id))
            await db.commit()


@pytest.mark.asyncio
async def test_url_edit_during_audit_rejects_fetched_evidence(monkeypatch):
    job_id = f"test_dated_audit_race_{uuid.uuid4().hex}"
    url_a = "https://www.winchester.ac.uk/study/Postgraduate/Courses/MA-Race-A-2025/"
    url_b = "https://www.winchester.ac.uk/study/Postgraduate/Courses/MA-Race-B-2025/"
    started = asyncio.Event()
    release = asyncio.Event()

    async def blocked_audit(_url):
        started.set()
        await release.wait()
        return {
            "checkedAt": "2026-07-23T00:00:00Z",
            "original": {"url": url_a, "verified": True},
            "candidate": None,
            "suggestion": None,
            "reason": "uncertain",
            "referenceOnly": True,
        }

    monkeypatch.setattr(
        "app.services.winchester_catalogue_review.audit_dated_route",
        blocked_audit,
    )
    async with AsyncSessionLocal() as db:
        university_id = (await db.execute(select(University.id).limit(1))).scalar_one()
        row = ScrapedCourse(
            scrape_job_id=job_id,
            university_id=university_id,
            course_name="MA Audit Race",
            course_website=url_a,
            scrape_warnings=["dated_catalogue_page_review"],
        )
        db.add(row)
        await db.commit()
        await db.refresh(row)
        row_id = row.id
    body = DatedCatalogueAuditBody(
        universityId=university_id,
        rows=[{"id": row_id, "jobId": job_id}],
    )
    try:
        async with AsyncSessionLocal() as audit_db:
            audit_task = asyncio.create_task(audit_dated_catalogue_reviews(body, audit_db, {}))
            await started.wait()
            async with AsyncSessionLocal() as edit_db:
                await staged_update(row_id, edit_db, {"courseWebsite": url_b})
            release.set()
            with pytest.raises(HTTPException) as conflict:
                await audit_task
            assert conflict.value.status_code == 409
        async with AsyncSessionLocal() as db:
            reloaded = await db.get(ScrapedCourse, row_id)
            assert reloaded.course_website == url_b
            assert "dated_catalogue_review" not in (reloaded.extraction_method or {})
    finally:
        release.set()
        async with AsyncSessionLocal() as db:
            await db.execute(delete(ScrapedCourse).where(ScrapedCourse.scrape_job_id == job_id))
            await db.commit()


@pytest.mark.asyncio
async def test_any_url_change_marks_evidence_stale_and_blocks_later_decision(monkeypatch):
    job_id = f"test_dated_stale_{uuid.uuid4().hex}"
    url_a = "https://www.winchester.ac.uk/study/Postgraduate/Courses/MA-Stale-A-2025/"
    url_b = "https://www.winchester.ac.uk/study/Postgraduate/Courses/MA-Stale-B-2025/"

    async def official_audit(_url):
        return {
            "checkedAt": "2026-07-24T00:00:00Z",
            "original": {"url": url_a, "verified": True},
            "candidate": {"url": url_a.replace("-2025", ""), "verified": True},
            "suggestion": "current_counterpart",
            "reason": "verified",
            "referenceOnly": True,
        }

    monkeypatch.setattr(
        "app.services.winchester_catalogue_review.audit_dated_route",
        official_audit,
    )
    async with AsyncSessionLocal() as db:
        university_id = (await db.execute(select(University.id).limit(1))).scalar_one()
        row = ScrapedCourse(
            scrape_job_id=job_id,
            university_id=university_id,
            course_name="MA Stale",
            course_website=url_a,
            scrape_warnings=["dated_catalogue_page_review"],
        )
        db.add(row)
        await db.commit()
        await db.refresh(row)
        row_id = row.id
    body = DatedCatalogueAuditBody(
        universityId=university_id,
        rows=[{"id": row_id, "jobId": job_id}],
    )
    try:
        async with AsyncSessionLocal() as db:
            audited = await audit_dated_catalogue_reviews(body, db, {})
        audited_revision = audited["rows"][0]["review"]["revision"]
        # Simulate another legitimate write path, not the staged edit route.
        async with AsyncSessionLocal() as db:
            row = await db.get(ScrapedCourse, row_id)
            row.course_website = url_b
            await db.commit()
        async with AsyncSessionLocal() as db:
            read_back = await read_dated_catalogue_reviews(body, db, {})
            assert read_back["rows"][0]["review"]["evidenceStale"] is True
            assert read_back["rows"][0]["review"]["decision"] is None
        async with AsyncSessionLocal() as db:
            with pytest.raises(HTTPException) as conflict:
                await decide_dated_catalogue_review(
                    row_id,
                    DatedCatalogueDecisionBody(
                        universityId=university_id,
                        jobId=job_id,
                        expectedRevision=audited_revision,
                        decision="current_counterpart",
                    ),
                    db,
                    {"id": 8, "email": "reviewer@example.test", "name": "Reviewer"},
                )
            assert conflict.value.status_code == 409
        # The ordinary edit route also invalidates revision/decision and appends history.
        async with AsyncSessionLocal() as db:
            await staged_update(row_id, db, {"courseWebsite": url_a})
        async with AsyncSessionLocal() as db:
            row = await db.get(ScrapedCourse, row_id)
            stored = row.extraction_method["dated_catalogue_review"]
            assert stored["revision"] == audited_revision + 1
            assert stored["decision"] is None
            assert stored["history"][-1]["type"] == "source_url_changed"
    finally:
        async with AsyncSessionLocal() as db:
            await db.execute(delete(ScrapedCourse).where(ScrapedCourse.scrape_job_id == job_id))
            await db.commit()


@pytest.mark.asyncio
async def test_two_real_reviewers_cannot_overwrite_same_revision():
    job_id = f"test_dated_two_reviewers_{uuid.uuid4().hex}"
    source_url = "https://www.winchester.ac.uk/study/Postgraduate/Courses/MA-Two-Reviewers-2025/"
    async with AsyncSessionLocal() as db:
        university_id = (await db.execute(select(University.id).limit(1))).scalar_one()
        row = ScrapedCourse(
            scrape_job_id=job_id,
            university_id=university_id,
            course_name="MA Two Reviewers",
            course_website=source_url,
            scrape_warnings=["dated_catalogue_page_review"],
            extraction_method={
                "dated_catalogue_review": {
                    "revision": 1,
                    "sourceUrl": source_url,
                    "sourceFingerprint": _dated_source_fingerprint(source_url),
                    "evidence": {
                        "checkedAt": "2026-07-25T00:00:00Z",
                        "suggestion": None,
                    },
                    "history": [],
                },
            },
        )
        db.add(row)
        await db.commit()
        await db.refresh(row)
        row_id = row.id

    async def decide(decision: str, reviewer_id: int):
        async with AsyncSessionLocal() as db:
            try:
                result = await decide_dated_catalogue_review(
                    row_id,
                    DatedCatalogueDecisionBody(
                        universityId=university_id,
                        jobId=job_id,
                        expectedRevision=1,
                        decision=decision,
                    ),
                    db,
                    {
                        "id": reviewer_id,
                        "email": f"reviewer{reviewer_id}@example.test",
                        "name": f"Reviewer {reviewer_id}",
                    },
                )
                return ("saved", result["review"]["decision"])
            except HTTPException as exc:
                return ("rejected", exc.status_code)

    try:
        outcomes = await asyncio.gather(
            decide("keep_separate", 1),
            decide("not_counterpart", 2),
        )
        assert sorted(outcome[0] for outcome in outcomes) == ["rejected", "saved"]
        assert ("rejected", 409) in outcomes
        async with AsyncSessionLocal() as db:
            reloaded = await db.get(ScrapedCourse, row_id)
            stored = reloaded.extraction_method["dated_catalogue_review"]
            assert stored["revision"] == 2
            assert len([event for event in stored["history"] if event["type"] == "reviewer_decision"]) == 1
    finally:
        async with AsyncSessionLocal() as db:
            await db.execute(delete(ScrapedCourse).where(ScrapedCourse.scrape_job_id == job_id))
            await db.commit()