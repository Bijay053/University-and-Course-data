"""Synthetic PostgreSQL scale gates; all fixtures/DDL roll back via the shared db fixture.

No external fetches or production load. Gates include HTTP JSON encoding, not
just the hash helper: 240 rows with >=64 KiB persisted evidence each, six source
records each, 25/100-row pages, and 240-row trigger-driven bulk updates.
"""
import json
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from time import perf_counter

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import event, select, text

from app.dependencies import get_current_user, get_db
from app.models import ScrapedCourse, ScrapedFieldEvidence
from app.routers import scrape
from app.services.scraper.approval_guidance import (
    attempt_identity, public_approval_guidance, synchronize_updated_guidance,
)
from tests.test_ulaw_qualifications import db, seed


class Meter:
    def __init__(self, db):
        self.engine = db.bind.sync_engine
        self.queries = 0

    def count(self, *args):
        # Fixture transaction bookkeeping is not an application query.
        if not args[2].lstrip().upper().startswith(("SAVEPOINT", "RELEASE SAVEPOINT", "ROLLBACK TO SAVEPOINT")):
            self.queries += 1

    def __enter__(self):
        self.queries = 0
        event.listen(self.engine, "before_cursor_execute", self.count)
        self.started = perf_counter()
        return self

    def __exit__(self, *args):
        self.elapsed = perf_counter() - self.started
        event.remove(self.engine, "before_cursor_execute", self.count)


async def large_rows(db):
    uni, job, control_id = await seed(db)
    # Nested source material, historical candidates, and legacy diagnostics.
    evidence = {"candidates": [
        {"field": field, "sourceUrl": f"https://synthetic.test/{i}",
         "snippet": (f"Published {field} for international applicants {i}. " * 110),
         "confidence": 0.95, "proofcontract_sha256": f"public-proof-{i}",
         "legacy": [{"last_qualification_approval": {"secret": "PRIVATE-DIAGNOSTIC"},
                     "lastQualificationApproval": {"secret": "PRIVATE-DIAGNOSTIC"}}]}
        for i, field in enumerate(["fee", "duration", "ielts", "location"] * 4)
    ]}
    assert len(json.dumps(evidence).encode()) >= 65_536
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    rows = [ScrapedCourse(
        university_id=uni, scrape_job_id=job, course_name=f"Synthetic qualification {i}",
        course_website=f"https://synthetic.test/course/{i}", status="pending",
        international_fee=18000, currency="GBP", fee_term="Annual",
        degree_level="Postgraduate", study_mode="On Campus", study_load="Full Time",
        extraction_method=deepcopy(evidence), notes="original",
        created_at=start + timedelta(seconds=i),
    ) for i in range(240)]
    db.add_all(rows)
    await db.flush()
    db.add_all([ScrapedFieldEvidence(
        scraped_course_id=row.id, field_key=field, source_url=row.course_website,
        snippet=f"Official {field} evidence " * 20, extraction_method="static",
    ) for row in rows for field in (
        "international_fee", "duration", "ielts_overall", "study_mode", "course_location", "intake_months"
    )])
    await db.commit()
    # Fingerprints must use round-tripped values, never constructor defaults.
    db.expire_all()
    rows = list((await db.execute(select(ScrapedCourse).where(
        ScrapedCourse.scrape_job_id == job, ScrapedCourse.id != control_id,
    ).order_by(ScrapedCourse.id))).scalars())
    return uni, job, control_id, rows


def guidance(row):
    return {**attempt_identity(row), "reasonCode": "changed_cohort",
            "attemptedAt": "2026-09-27T00:00:00+00:00"}


@pytest.mark.asyncio
async def test_large_guided_review_pages_and_bulk_invalidation(db):
    uni, job, control_id, rows = await large_rows(db)
    expected_ids = {control_id, *(row.id for row in rows)}
    app = FastAPI()
    app.include_router(scrape.router, prefix="/api/scrape")
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: {"permissions": ["staged.view"]}
    metrics = []
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                base_url="http://test") as client:
        async def full_review():
            db.expire_all()
            with Meter(db) as meter:
                response = await client.get(f"/api/scrape/staged/{job}")
            assert response.status_code == 200, response.text[:500]
            assert meter.elapsed < 5
            assert meter.queries <= 6
            assert len(response.content) < 241 * 200_000
            for private in ("PRIVATE-DIAGNOSTIC", "evidenceFingerprint", "last_qualification_approval"):
                assert private not in response.text
            payload = response.json()
            assert payload["lastScrape"]["jobId"] == job
            assert len(payload["courses"]) == 241
            assert {row["id"] for row in payload["courses"]} == expected_ids
            metrics.append({"operation": "full-review-241",
                            "seconds": round(meter.elapsed, 3),
                            "queries": meter.queries, "bytes": len(response.content)})
            return payload["courses"], len(response.content), meter.queries

        async def page(limit, number=1):
            # Expire before every request so no warm ORM identity data hides IO.
            db.expire_all()
            with Meter(db) as meter:
                response = await client.get("/api/scrape/staged", params={
                    "jobId": job, "universityId": uni, "status": "pending",
                    "limit": limit, "page": number,
                })
            assert response.status_code == 200, response.text[:500]
            assert meter.elapsed < 5.0
            assert meter.queries == 5
            assert len(response.content) < limit * 200_000
            assert "PRIVATE-DIAGNOSTIC" not in response.text
            assert "evidenceFingerprint" not in response.text
            assert "last_qualification_approval" not in response.text
            metrics.append({"operation": f"page-{limit}-{number}",
                            "seconds": round(meter.elapsed, 3), "queries": meter.queries,
                            "bytes": len(response.content)})
            return response.json(), len(response.content), meter.queries

        full_baseline, full_base_bytes, full_base_queries = await full_review()
        assert all(row["lastQualificationApproval"] is None for row in full_baseline)
        baseline, base_bytes, base_queries = await page(100, 2)
        # Reload all rows after expiry, then add metadata without changing evidence.
        rows = list((await db.execute(select(ScrapedCourse).where(
            ScrapedCourse.scrape_job_id == job, ScrapedCourse.id != control_id,
        ).order_by(ScrapedCourse.id))).scalars())
        with Meter(db) as meter:
            for row in rows:
                row.last_qualification_approval = guidance(row)
            await db.commit()
        assert meter.elapsed < 5
        metrics.append({"operation": "save-240", "seconds": round(meter.elapsed, 3)})
        full_guided, full_bytes, full_queries = await full_review()
        assert full_queries == full_base_queries
        assert 0 < full_bytes - full_base_bytes < 240 * 300
        assert [r["id"] for r in full_guided] == [r["id"] for r in full_baseline]
        for item in full_guided:
            if item["id"] == control_id:
                assert item["lastQualificationApproval"] is None
                continue
            assert item["lastQualificationApproval"] == {
                "rowId": item["id"], "jobId": job, "universityId": uni,
                "reasonCode": "changed_cohort", "attemptedAt": "2026-09-27T00:00:00+00:00",
            }
            assert len(item["evidence"]) == 6
        guided, guided_bytes, queries = await page(100, 2)
        assert queries == base_queries
        assert [r["id"] for r in guided] == [r["id"] for r in baseline]
        assert 0 < guided_bytes - base_bytes < 100 * 300
        small, _, small_queries = await page(25, 2)
        assert small_queries == queries
        for item in guided + small:
            assert item["lastQualificationApproval"] == {
                "rowId": item["id"], "jobId": job, "universityId": uni,
                "reasonCode": "changed_cohort", "attemptedAt": "2026-09-27T00:00:00+00:00",
            }
            assert len(item["evidence"]) == 6
        next_page, _, _ = await page(100, 3)
        assert len(next_page) == 41  # includes the original unguided control
        assert not ({r["id"] for r in guided} & {r["id"] for r in next_page})

    # Loaded identities test the explicit DML synchronization path as well as
    # PostgreSQL's full-row JSON comparison trigger.
    rows = list((await db.execute(select(ScrapedCourse).where(
        ScrapedCourse.scrape_job_id == job, ScrapedCourse.id != control_id,
    ))).scalars())
    control = await db.get(ScrapedCourse, control_id)
    control.last_qualification_approval = guidance(control)
    await db.commit()
    control_saved = deepcopy(control.last_qualification_approval)
    ids = [r.id for r in rows]
    for label, assignment in (("noop", "notes"), ("change", "'changed'"), ("restore", "'original'")):
        with Meter(db) as meter:
            result = await db.execute(text(
                f"UPDATE scraped_courses SET notes={assignment} "
                "WHERE id = ANY(:ids) AND scrape_job_id=:job AND university_id=:uni RETURNING id"
            ), {"ids": ids, "job": job, "uni": uni})
            assert set(await synchronize_updated_guidance(db, result)) == set(ids)
            await db.commit()
        assert meter.queries == 2  # UPDATE and one batch SELECT, independent of row count
        assert meter.elapsed < 5
        metrics.append({"operation": label, "seconds": round(meter.elapsed, 3),
                        "queries": meter.queries})
        assert all(bool(r.last_qualification_approval) == (label == "noop") for r in rows)
        await db.refresh(control)
        assert control.last_qualification_approval == control_saved
    db.expire_all()
    rows = list((await db.execute(select(ScrapedCourse).where(ScrapedCourse.id.in_(ids)))).scalars())
    assert all(r.notes == "original" and public_approval_guidance(r) is None for r in rows)
    # Even valid fingerprints cannot cross any of the three identity fences.
    for key, value in (("rowId", control_id), ("jobId", "other-job"), ("universityId", uni + 1)):
        rows[0].last_qualification_approval = {**guidance(rows[0]), key: value}
        assert public_approval_guidance(rows[0]) is None
    print("\nGUIDANCE_SCALE " + json.dumps(metrics))