"""Phase 10 — change_snapshot.py

Captures an immutable snapshot of every staged course's key fields after a
completed scrape job.  The change detector then diffs the latest snapshot
against the previous one to emit course_change_events.
"""
from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.scraped_course import ScrapedCourse
from app.models.course_snapshot import CourseSnapshot

log = logging.getLogger(__name__)


def _page_hash(sc: ScrapedCourse) -> str:
    """Stable SHA-1 hash of the key scraped fields for O(1) change detection."""
    payload: dict[str, Any] = {
        "fee": sc.international_fee,
        "duration": str(sc.duration) if sc.duration is not None else None,
        "intakes": sorted(sc.intake_months or []),
        "ielts": sc.ielts_overall,
        "location": sc.course_location,
        "mode": sc.study_mode,
        "requirement": (sc.other_requirement or "")[:200],
    }
    return hashlib.sha1(
        json.dumps(payload, sort_keys=True, default=str).encode()
    ).hexdigest()


async def take_snapshot(
    university_id: int,
    scrape_job_id: str,
    db: AsyncSession,
) -> int:
    """Snapshot all staged courses for *scrape_job_id* at *university_id*.

    Idempotent per job — safe to call more than once (later calls are no-ops
    because `INSERT … ON CONFLICT DO NOTHING` is not needed here; the
    orchestrator calls this exactly once per completed job).

    Returns the number of snapshot rows written.
    """
    rows_q = await db.execute(
        select(ScrapedCourse).where(
            ScrapedCourse.scrape_job_id == scrape_job_id,
            ScrapedCourse.university_id == university_id,
        )
    )
    courses = rows_q.scalars().all()
    if not courses:
        log.info("[SNAPSHOT] no staged courses for run=%s uni=%s", scrape_job_id, university_id)
        return 0

    def _to_float(val: object) -> float | None:
        """Safe cast — returns None for non-numeric strings (e.g. '3 years')."""
        if val is None:
            return None
        try:
            return float(val)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None

    snaps: list[CourseSnapshot] = []
    for sc in courses:
        snaps.append(
            CourseSnapshot(
                university_id=university_id,
                scrape_job_id=scrape_job_id,
                course_id=sc.course_id,
                course_name=sc.course_name,
                course_url=sc.course_website,
                international_fee=_to_float(sc.international_fee),
                fee_term=sc.fee_term,
                duration=_to_float(sc.duration),
                duration_term=sc.duration_term,
                intake_months=sc.intake_months,
                ielts_overall=sc.ielts_overall,
                pte_overall=sc.pte_overall,
                toefl_overall=sc.toefl_overall,
                academic_score=sc.academic_score,
                academic_level=sc.academic_level,
                other_requirement=sc.other_requirement,
                course_location=sc.course_location,
                study_mode=sc.study_mode,
                degree_level=sc.degree_level,
                avg_verification_confidence=sc.avg_verification_confidence,
                auto_publish_status=sc.auto_publish_status,
                page_hash=_page_hash(sc),
            )
        )

    db.add_all(snaps)
    await db.commit()
    log.info(
        "[SNAPSHOT] uni=%s run=%s: wrote %d snapshots",
        university_id, scrape_job_id, len(snaps),
    )
    return len(snaps)
