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


@pytest.mark.parametrize("scope", [False, True, None])
def test_summary_preserves_legacy_fee_authority_and_scope_value(scope):
    authority = {
        "status": "range",
        "selected": [{"amount": 18000, "source_url": "https://synthetic.test/fee",
                      "snippet": "Official campus fee"}],
    }
    metadata = {
        "fee_variants": authority,
        "ulaw_qualification_scope": scope,
        "campus_fee_scope": {
            "locations": ["Birmingham", "Leeds"],
            "split_from_id": 17,
            "original_name": "MSc Healthcare Management",
            "private_evidence": "bulky-campus-source",
        },
        "raw_data": {"private": "bulky-source"},
    }
    row = {
        "id": 17, "extraction_method": metadata, "extractionMethod": metadata,
        "rawData": {"private": "bulky-source"}, "raw_data": {"private": "bulky-source"},
        "evidence": [{"snippet": "other bulky source"}],
        "feeSelection": {"selectedOptionId": None},
    }
    result = scrape._staged_summary(row)
    assert result["feeVariants"] == authority
    assert "fee_variants" not in result  # legacy authority promoted out of metadata
    assert result["extractionMethod"] == {
        "ulaw_qualification_scope": scope,
        "campus_fee_scope": {
            "locations": ["Birmingham", "Leeds"],
            "split_from_id": 17,
            "original_name": "MSc Healthcare Management",
        },
    }
    assert result["feeSelection"] == {"selectedOptionId": None}
    assert not {"extraction_method", "raw_data", "rawData", "evidence"} & result.keys()
    assert metadata["raw_data"] == {"private": "bulky-source"}  # no ORM JSON mutation


def test_summary_without_scope_does_not_invent_one():
    row = {"id": 17, "extraction_method": {"raw_data": "large"}, "evidence": []}
    assert "extractionMethod" not in scrape._staged_summary(row)


def test_summary_projects_nested_qualification_scope_truthiness_without_proof():
    proof = "VERIFIED-SOURCE-PRIVATE-" * 4096
    nested_scope = {
        "award": "PG Dip",
        "pre_split": {"extraction_method": {"sources": [proof]}},
        "verified_source": {"html": proof},
    }
    for scope, expected in ((nested_scope, True), ({}, False), (False, False),
                            (None, None)):
        row = {"id": 17, "extraction_method": {"ulaw_qualification_scope": scope}}
        full_bytes = len(json.dumps(row).encode())
        projected = scrape._staged_summary(row)
        assert projected["extractionMethod"] == {"ulaw_qualification_scope": expected}
        if scope is nested_scope:
            assert full_bytes >= 65_536
            assert len(json.dumps(projected).encode()) < 200
            assert "VERIFIED-SOURCE-PRIVATE" not in json.dumps(projected)


def test_summary_does_not_replace_existing_top_level_fee_authority():
    top_level = {"status": "uniform", "selected": [{"amount": 19000}]}
    row = {
        "id": 17, "feeVariants": top_level,
        "extraction_method": {"fee_variants": {"status": "range", "selected": []}},
    }
    assert scrape._staged_summary(row)["feeVariants"] == top_level


@pytest.mark.asyncio
async def test_api_summary_keeps_legacy_fee_authority_and_minimal_campus_scope(db):
    uni, job, row_id = await seed(db)
    row = await db.get(ScrapedCourse, row_id)
    original_name = "MSc Healthcare Management"
    authority = {
        "status": "uniform",
        "selected": [{"amount": 18000, "source_url": "https://synthetic.test/fee",
                      "study_variant": "MSc", "snippet": "Official Birmingham and Leeds fee"}],
    }
    row.course_name = f"{original_name} — Birmingham"
    row.course_location = "Birmingham"
    row.status = "pending"
    row.extraction_method = {
        "fee_variants": authority,
        "ulaw_qualification_scope": {
            "award": "PG Dip",
            "pre_split": {"extraction_method": {
                "source": "NESTED-PRE-SPLIT-PRIVATE-" * 4096,
            }},
            "verified_source": {"html": "NESTED-VERIFIED-PRIVATE-" * 4096},
        },
        "campus_fee_scope": {
            "locations": ["Birmingham"],
            "split_from_id": row_id,
            "original_name": original_name,
            "internal_source_evidence": "PRIVATE-SCOPE-PROOF",
        },
        "candidates": [{"snippet": "PRIVATE-RAW-SOURCE"}],
    }
    sibling = ScrapedCourse(
        university_id=uni, scrape_job_id=job,
        course_name=f"{original_name} — Leeds", course_website=row.course_website,
        course_location="Leeds", degree_level=row.degree_level, study_mode=row.study_mode,
        international_fee=18000, fee_term=row.fee_term, fee_year=row.fee_year,
        currency=row.currency, fee_scope_key="leeds", status="pending",
        extraction_method={
            "fee_variants": deepcopy(authority),
            "campus_fee_scope": {
                "locations": ["Leeds"], "split_from_id": row_id,
                "original_name": original_name,
                "private_evidence": "PRIVATE-SIBLING-PROOF",
            },
        },
    )
    db.add(sibling)
    await db.commit()
    app = FastAPI()
    app.include_router(scrape.router, prefix="/api/scrape")
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: {"permissions": ["staged.view"]}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                base_url="http://test") as client:
        for path, params in (
            ("/api/scrape/staged", {"jobId": job, "universityId": uni}),
            (f"/api/scrape/staged/{job}", {}),
        ):
            full = (await client.get(path, params=params)).json()
            summary_response = await client.get(path, params={**params, "view": "summary"})
            assert summary_response.status_code == 200
            assert len(summary_response.content) < 0.5 * len(json.dumps(full).encode())
            summary = summary_response.json()
            full_rows = full["courses"] if isinstance(full, dict) else full
            items = summary["courses"] if isinstance(summary, dict) else summary
            assert {item["id"] for item in items} == {row_id, sibling.id}
            assert [item["id"] for item in items] == [item["id"] for item in full_rows]
            for item, full_row in zip(items, full_rows):
                locations = ["Birmingham"] if item["id"] == row_id else ["Leeds"]
                assert item["feeVariants"] == full_row["extractionMethod"]["fee_variants"]
                expected_metadata = {
                    "campus_fee_scope": {
                        "locations": locations, "split_from_id": row_id,
                        "original_name": original_name,
                    },
                }
                if item["id"] == row_id:
                    expected_metadata["ulaw_qualification_scope"] = True
                assert item["extractionMethod"] == expected_metadata
                assert item["feeSelection"] == full_row["feeSelection"]
                assert "extraction_method" not in item
            assert "PRIVATE-SCOPE-PROOF" not in summary_response.text
            assert "PRIVATE-RAW-SOURCE" not in summary_response.text
            assert "PRIVATE-SIBLING-PROOF" not in summary_response.text
            assert "NESTED-PRE-SPLIT-PRIVATE" not in summary_response.text
            assert "NESTED-VERIFIED-PRIVATE" not in summary_response.text


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
        explicit_full = await client.get("/api/scrape/staged", params={
            "jobId": job, "universityId": uni, "status": "pending",
            "limit": 100, "page": 2, "view": "full",
        })
        assert explicit_full.status_code == 200
        assert explicit_full.json() == baseline
        explicit_job_full = await client.get(f"/api/scrape/staged/{job}", params={"view": "full"})
        assert explicit_job_full.status_code == 200
        assert explicit_job_full.json()["courses"] == full_baseline
        for path, params, full_items in (
            ("/api/scrape/staged", {"jobId": job, "universityId": uni,
                                    "status": "pending", "limit": 100, "page": 2}, baseline),
            (f"/api/scrape/staged/{job}", {}, full_baseline),
        ):
            db.expire_all()
            with Meter(db) as meter:
                response = await client.get(path, params={**params, "view": "summary"})
            assert response.status_code == 200, response.text[:500]
            assert meter.elapsed < 5
            assert meter.queries <= 6
            summary = response.json()
            items = summary["courses"] if isinstance(summary, dict) else summary
            assert [item["id"] for item in items] == [item["id"] for item in full_items]
            assert len(response.content) < 0.2 * (
                full_base_bytes if isinstance(summary, dict) else base_bytes
            )
            for item, full in zip(items, full_items):
                assert item["evidenceCount"] == len(full["evidence"])
                assert item["evidenceLoaded"] is False
                assert not {"evidence", "extraction_method", "raw_data", "rawData"} & item.keys()
                for key in ("courseName", "status", "feeSelection", "requirementStatus",
                            "durationReviewStatus", "lastQualificationApproval", "recoveryCount"):
                    assert item[key] == full[key]
            assert "PRIVATE-DIAGNOSTIC" not in response.text
            metrics.append({"operation": "summary-review-241" if isinstance(summary, dict)
                            else "summary-page-100", "seconds": round(meter.elapsed, 3),
                            "queries": meter.queries, "bytes": len(response.content)})

        target = baseline[0]
        detail_url = f"/api/scrape/staged/{target['id']}/evidence"
        fences = {"jobId": job, "universityId": uni}
        for params in ({}, {"jobId": job}, {"universityId": uni}):
            assert (await client.get(detail_url, params=params)).status_code == 422
        for params in ({"jobId": "wrong", "universityId": uni},
                       {"jobId": job, "universityId": uni + 1}):
            assert (await client.get(detail_url, params=params)).status_code == 404
        detail = await client.get(detail_url, params=fences)
        assert detail.status_code == 200, detail.text[:500]
        assert detail.json()["course"] == target
        assert "PRIVATE-DIAGNOSTIC" not in detail.text
        history_url = f"/api/scrape/history/{job}"
        db.expire_all()
        with Meter(db) as meter:
            history_full_response = await client.get(history_url)
        assert history_full_response.status_code == 200, history_full_response.text[:500]
        history_full = history_full_response.json()
        history_explicit = await client.get(history_url, params={"view": "full"})
        assert history_explicit.status_code == 200
        assert history_explicit.json() == history_full
        assert {item["id"] for item in history_full["stagedCourses"]} == expected_ids
        metrics.append({"operation": "history-full-241",
                        "seconds": round(meter.elapsed, 3),
                        "queries": meter.queries, "bytes": len(history_full_response.content)})
        db.expire_all()
        with Meter(db) as meter:
            history_summary_response = await client.get(history_url, params={"view": "summary"})
        assert history_summary_response.status_code == 200, history_summary_response.text[:500]
        history_summary = history_summary_response.json()
        assert history_summary.keys() == history_full.keys()
        for key in ("job", "logs", "provider_failure", "unresolvedCourses"):
            assert history_summary[key] == history_full[key]
        assert len(history_summary_response.content) < len(history_full_response.content) * 0.5
        assert meter.elapsed < 5
        assert meter.queries <= 10
        assert [item["id"] for item in history_summary["stagedCourses"]] == [
            item["id"] for item in history_full["stagedCourses"]
        ]
        for item, full in zip(history_summary["stagedCourses"], history_full["stagedCourses"]):
            assert item["scrapeJobId"] == job and item["universityId"] == uni
            assert item["evidenceLoaded"] is False
            assert item["evidenceCount"] == len(full["evidence"])
            assert "evidence" not in item
            for key in full.keys() - {"evidence"}:
                assert item[key] == full[key]
        assert (await client.get(history_url, params={"view": "invalid"})).status_code == 422
        assert (await client.get("/api/scrape/history/nonexistent-job",
                                 params={"view": "summary"})).status_code == 404
        metrics.append({"operation": "history-summary-241",
                        "seconds": round(meter.elapsed, 3),
                        "queries": meter.queries, "bytes": len(history_summary_response.content)})
        # A second university/job must not make its staged row visible in the
        # selected job's history, even when the reviewer requests summary.
        other_uni, other_job, other_id = await seed(db)
        other_history = await client.get(f"/api/scrape/history/{other_job}",
                                         params={"view": "summary"})
        assert other_history.status_code == 200
        assert [row["id"] for row in other_history.json()["stagedCourses"]] == [other_id]
        assert other_history.json()["stagedCourses"][0]["universityId"] == other_uni
        original_history = await client.get(history_url, params={"view": "summary"})
        assert [row["id"] for row in original_history.json()["stagedCourses"]] == [
            row["id"] for row in history_full["stagedCourses"]
        ]
        app.dependency_overrides[get_current_user] = lambda: {"permissions": []}
        assert (await client.get(detail_url, params=fences)).status_code == 403
        assert (await client.get("/api/scrape/staged", params={**fences, "view": "summary"})).status_code == 403
        app.dependency_overrides[get_current_user] = lambda: {"permissions": ["staged.view"]}
        assert (await client.get("/api/scrape/staged", params={**fences, "view": "invalid"})).status_code == 422
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