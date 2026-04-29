"""Priority 5 — Component 1: per-field, per-method fill-rate metrics.

After every scrape run completes, ``compute_run_metrics`` aggregates
``scraped_field_evidence`` rows for that run and writes per-(field, method)
counts into ``scrape_run_metrics``.  The fill rate is ``count / courses_total``
where the denominator is the number of successfully staged courses.

Tracked fields — everything that has evidence rows including:
  English:  ielts_overall, ielts_listening, ielts_reading, ielts_speaking,
            ielts_writing, toefl_overall, toefl_listening, toefl_reading,
            toefl_speaking, toefl_writing, pte_overall, cambridge_overall,
            duolingo_overall
  Fees:     international_fee, domestic_fee, fee_term, fee_year, fee_currency
  Details:  duration, duration_value, duration_unit, intake_months,
            course_location, study_mode, mode
  Labels:   category, sub_category, degree_level
"""
from __future__ import annotations

import logging

from sqlalchemy import distinct, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.scraped_course import ScrapedCourse
from app.models.evidence import ScrapedFieldEvidence
from app.models.scrape_run_metrics import ScrapeRunMetrics

log = logging.getLogger(__name__)

TRACKED_FIELDS: frozenset[str] = frozenset({
    # English — overall
    "ielts_overall",
    "toefl_overall",
    "pte_overall",
    "cambridge_overall",
    "duolingo_overall",
    # English — sub-bands
    "ielts_listening", "ielts_reading", "ielts_speaking", "ielts_writing",
    "toefl_listening", "toefl_reading", "toefl_speaking", "toefl_writing",
    "pte_listening", "pte_reading", "pte_speaking", "pte_writing",
    # Fees
    "international_fee", "domestic_fee", "fee_term", "fee_year", "fee_currency",
    # Course details
    "duration", "duration_value", "duration_unit",
    "intake_months",
    "course_location", "study_mode", "mode",
    # Classification
    "category", "sub_category", "degree_level",
})


async def compute_run_metrics(
    db: AsyncSession,
    scrape_run_id: str,
    university_id: int,
) -> int:
    """Compute per-field per-method fill-rate metrics for a completed scrape run.

    Returns the number of metric rows written (0 if there are no staged courses).
    Safe to call multiple times — existing rows for the run are deleted first so
    a retry after a transient failure produces clean results.
    """
    # Count successfully-staged courses for this run (the fill-rate denominator)
    courses_total_result = await db.execute(
        select(func.count(ScrapedCourse.id))
        .where(ScrapedCourse.scrape_job_id == scrape_run_id)
        .where(ScrapedCourse.status.in_(["pending", "review_ready"]))
    )
    courses_total: int = courses_total_result.scalar() or 0

    if courses_total == 0:
        log.info("[METRICS] run %s: 0 staged courses — skipping metrics", scrape_run_id)
        return 0

    # Delete any stale rows from a previous (failed) attempt
    from sqlalchemy import delete as sa_delete
    await db.execute(
        sa_delete(ScrapeRunMetrics).where(ScrapeRunMetrics.scrape_run_id == scrape_run_id)
    )

    # Aggregate: for each (field_key, extraction_method), count distinct
    # courses that have at least one selected evidence row for that slot.
    rows_result = await db.execute(
        select(
            ScrapedFieldEvidence.field_key,
            ScrapedFieldEvidence.extraction_method.label("method"),
            func.count(distinct(ScrapedFieldEvidence.scraped_course_id)).label("count"),
        )
        .join(ScrapedCourse, ScrapedCourse.id == ScrapedFieldEvidence.scraped_course_id)
        .where(ScrapedCourse.scrape_job_id == scrape_run_id)
        .where(ScrapedFieldEvidence.selected.is_(True))
        .group_by(ScrapedFieldEvidence.field_key, ScrapedFieldEvidence.extraction_method)
    )
    rows = rows_result.all()

    metrics: list[ScrapeRunMetrics] = []
    for row in rows:
        field_key: str = row.field_key or ""
        method: str = row.method or "unknown"
        count: int = row.count

        metrics.append(
            ScrapeRunMetrics(
                scrape_run_id=scrape_run_id,
                university_id=university_id,
                field_key=field_key,
                method=method,
                count=count,
                courses_total=courses_total,
                fill_rate=round(count / courses_total, 4),
            )
        )

    db.add_all(metrics)
    await db.commit()

    log.info(
        "[METRICS] run %s: wrote %d metric rows (%d fields, denominator=%d)",
        scrape_run_id,
        len(metrics),
        len({m.field_key for m in metrics}),
        courses_total,
    )
    return len(metrics)
