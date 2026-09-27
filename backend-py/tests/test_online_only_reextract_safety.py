"""Regression for online-only rediscovery of an already staged course."""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import delete, select

from app.database import AsyncSessionLocal, engine
from app.models import (
    CourseAuditLog, ScrapeFeedback, ScrapedCourse, ScrapedFieldEvidence, University,
)
from app.routers.scrape import ReExtractBody, re_extract_staged
from app.services.scraper.stage_course import quarantine_reextracted_online_course


@pytest.mark.parametrize("payload,url,expected", [
    ({"online_only": True}, "https://example.edu/course/msc-business", True),
    ({"study_mode": "Online", "course_location": "London"}, "https://example.edu/course/msc-business", True),
    ({"study_mode": "On Campus, Online"}, "https://example.edu/course/msc-business", False),
    ({}, "https://example.edu/course/msc-business-online", True),
    ({"course_name": "Master of Business ODL"}, "https://example.edu/course/business", True),
    ({"online_only_utas": True, "study_mode": "Blended", "course_location": "Hobart"}, "https://www.utas.edu.au/course/business", True),
    ({"study_mode": "On Campus", "course_location": None}, "https://www.utas.edu.au/course/business", True),
    ({"study_mode": "On Campus"}, "https://www.utas.edu.au/course/business", False),
])
def test_shared_online_policy_on_partial_extraction(payload, url, expected):
    from app.services.scraper.guards import is_online_only_for_staging

    assert is_online_only_for_staging(
        "Master of Business", payload, url, partial=True,
    ) is expected


@pytest.mark.asyncio
@pytest.mark.parametrize("use_real_source,additional_exclusion", [
    (False, None),
    (False, "part_time_only"),
    (False, "domestic_only"),
    (False, "utas_part_time_only"),
    (False, "utas_domestic_only"),
    (True, None),
])
async def test_reextract_quarantines_stale_campus_and_fee_without_deleting_review_row(
    monkeypatch, use_real_source, additional_exclusion,
):
    from app.services.scraper import central_pages, orchestrator
    from app.services.scraper.config import loader
    from app.services.scraper.config.context import current_uni_config
    from app.services.scraper.config.loader import load_uni_config
    from app.services.scraper.pipelines.single_course import extract_course
    from tests.test_ulaw_fees import POPULATION

    await engine.dispose()
    suffix = uuid.uuid4().hex[:12]
    source = POPULATION[41033] if use_real_source else None
    host = "www.utas.edu.au" if additional_exclusion and additional_exclusion.startswith("utas_") else "example.edu"
    url = source["url"] if source else f"https://{host}/course/msc-business-{suffix}"
    monkeypatch.setattr(central_pages, "prefetch_central_pages", _empty_central)
    # Do not generate an example.edu university recipe during this DB test.
    monkeypatch.setattr(loader, "get_config_for_host", lambda **kwargs: None)
    calls = []

    async def online_extract(*args, **kwargs):
        calls.append(args)
        if source:
            cfg = load_uni_config(
                slug="law_1902", name="University of Law",
                scrape_url="https://www.law.ac.uk/study/",
                create_missing_stub=False,
            )
            token = current_uni_config.set(cfg)
            try:
                return await extract_course(
                    url, html=source["html"], country="United Kingdom",
                    use_ai_fallback=False,
                )
            finally:
                current_uni_config.reset(token)
        return {
            "url": url,
            "payload": {
                "course_name": "Master of Business",
                "study_mode": "Blended" if host != "example.edu" else "Online",
                **({"online_only": True} if additional_exclusion is None else {}),
                **({"study_load": "Part Time"} if additional_exclusion and additional_exclusion.endswith("part_time_only") else {}),
                **({"domestic_only": True} if additional_exclusion and additional_exclusion.endswith("domestic_only") else {}),
                **({"online_only_utas": True, "course_location": "Hobart"} if host != "example.edu" else {}),
                # Deliberately no fresh fee or campus: old ones must not win.
            },
            "evidence": [],
        }

    monkeypatch.setattr(orchestrator, "_extract_only", online_extract)
    if source:
        from app.services.scraper.extractors import ulaw_fees

        async def no_fee_shortcut(*args, **kwargs):
            return None

        monkeypatch.setattr(ulaw_fees, "recover_course_fee_only", no_fee_shortcut)
    uni_id = None
    try:
        async with AsyncSessionLocal() as db:
            uni = University(
                name=f"Online re-extract {suffix}", country="United Kingdom",
                city="London",
                website="https://www.law.ac.uk" if source else "https://example.edu",
            )
            db.add(uni)
            await db.flush()
            uni_id = uni.id
            row = ScrapedCourse(
                university_id=uni_id, scrape_job_id=f"online_test_{suffix}",
                course_name="Master of Business", course_website=url,
                status="pending", international_fee=19050, fee_year=2025,
                course_location="London", study_mode="On Campus",
                auto_publish_status="ready",
                extraction_method={"fee_variants": {"status": "range"}},
            )
            db.add(row)
            await db.flush()
            row_id = row.id
            db.add(ScrapedFieldEvidence(
                scraped_course_id=row_id, field_key="international_fee",
                candidate_value="19050", selected=True,
            ))
            db.add(ScrapedFieldEvidence(
                scraped_course_id=row_id, field_key="course_location",
                candidate_value="London", selected=True,
            ))
            await db.commit()

            if additional_exclusion and host == "example.edu":
                from app.services.scraper.guards import should_stage_course

                _, gate_reason = should_stage_course(
                    row.course_name,
                    {
                        "course_name": row.course_name,
                        "study_mode": "Online",
                        "international_fee": 1,
                        **({"study_load": "Part Time"} if additional_exclusion == "part_time_only" else {}),
                        **({"domestic_only": True} if additional_exclusion == "domestic_only" else {}),
                    },
                    source_url=url,
                )
                assert gate_reason == "online_only"

            result = await re_extract_staged(
                ReExtractBody(
                    ids=[row_id], universityId=uni_id,
                    targetFields=["international_fee"],
                ), db,
            )
            assert result["results"][0]["outcome"] == "rejected_online_only"
            assert len(calls) == 1  # No retry can reintroduce stale evidence.
            await db.refresh(row)
            assert row.status == "rejected"
            assert row.rejection_reason == "online_only"
            assert row.course_id is None
            assert row.auto_publish_status != "ready"
            assert row.international_fee is None
            assert row.fee_year is None
            assert row.course_location is None
            assert row.on_campus_available is False
            assert "fee_variants" not in row.extraction_method
            assert "online_only_reextract" in row.scrape_warnings
            stale = (await db.execute(select(ScrapedFieldEvidence).where(
                ScrapedFieldEvidence.scraped_course_id == row_id,
                ScrapedFieldEvidence.field_key.in_(
                    ["international_fee", "course_location"]
                ),
            ))).scalars().all()
            assert stale == []
            feedback = (await db.execute(select(ScrapeFeedback).where(
                ScrapeFeedback.scraped_course_id == row_id,
                ScrapeFeedback.issue_type == "online_only_reextract",
            ))).scalars().all()
            assert len(feedback) == 1
            assert feedback[0].preferred_value == url
            audits = (await db.execute(select(CourseAuditLog).where(
                CourseAuditLog.scraped_course_id == row_id,
                CourseAuditLog.action == "staged_online_only_quarantine",
            ))).scalars().all()
            assert len(audits) == 1
            assert '"international_fee": 19050' in audits[0].old_value
            assert '"course_location": "London"' in audits[0].old_value
            # Repeating the same source finding is idempotent and audited once.
            assert await quarantine_reextracted_online_course(
                db, row, source_url=url,
            ) is False
            await db.commit()
            feedback = (await db.execute(select(ScrapeFeedback).where(
                ScrapeFeedback.scraped_course_id == row_id,
                ScrapeFeedback.issue_type == "online_only_reextract",
            ))).scalars().all()
            assert len(feedback) == 1
    finally:
        if uni_id is not None:
            async with AsyncSessionLocal() as db:
                await db.execute(delete(University).where(University.id == uni_id))
                await db.commit()


async def _empty_central(*args, **kwargs):
    return {}


@pytest.mark.asyncio
async def test_quarantined_online_only_row_cannot_be_force_approved():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from app.services.scraper.approve_course import (
        ApprovalValidationError, approve_scraped_course,
    )

    db = AsyncMock()
    row = SimpleNamespace(
        id=41, status="rejected", rejection_reason="online_only",
        course_name="Master of Business",
    )
    with pytest.raises(ApprovalValidationError, match="Online-only"):
        await approve_scraped_course(db, row)
    db.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_online_only_quarantine_does_not_modify_linked_or_approved_rows(
    monkeypatch,
):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from app.services.scraper import stage_course

    class _DB:
        scalar = AsyncMock(return_value=None)

        def __init__(self):
            self.added = []

        def add(self, value):
            self.added.append(value)

    db = _DB()
    row = SimpleNamespace(
        id=42, university_id=5, course_id=77, course_name="Master of Business",
        status="pending", international_fee=19050, course_location="London",
        auto_publish_status="ready",
    )
    refresh = AsyncMock(side_effect=AssertionError("published evidence changed"))
    monkeypatch.setattr(stage_course, "refresh_evidence_for_fields", refresh)
    assert not await stage_course.quarantine_reextracted_online_course(
        db, row, source_url="https://example.edu/course/business",
    )
    assert row.status == "pending"
    assert row.international_fee == 19050
    assert row.course_location == "London"
    assert row.auto_publish_status == "ready"
    assert db.added[0].issue_type == "online_only_reextract"
    assert "manual reconciliation" in db.added[0].reason
    row.course_id = None
    row.status = "approved"
    assert not await stage_course.quarantine_reextracted_online_course(
        db, row, source_url="https://example.edu/course/business",
    )
    assert row.status == "approved"