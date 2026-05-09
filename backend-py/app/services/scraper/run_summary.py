"""Week 2 — Prompt 1: wide-format scrape run summary.

After every scrape run completes, ``compute_run_summary`` aggregates
``scraped_courses``, ``scraped_field_evidence``, ``gemini_call_log`` and
``scrape_runtime_jobs`` into a single row in ``scrape_run_summary``.

Companion to ``metrics.compute_run_metrics`` — that one writes the
long-format per-(field, method) rows used by alerts/baselines; this one
writes the wide one-row-per-run shape consumed by the Week 2 dashboard
and alerting layer.

Skip-reason mapping
-------------------
``rejection_reason`` is a free-form text column. The orchestrator records
it via ``(res.reason or "unknown").replace(" ", "_").lower()[:40]`` so
values are best-effort tokens. We bucket them into the seven spec columns
with substring matching, falling back to ``skipped_other``.

Idempotency
-----------
``scrape_run_summary`` has a UNIQUE constraint on ``scrape_run_id``. We
DELETE before INSERT so retries after a transient failure produce a
single clean row.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Mapping

from sqlalchemy import delete as sa_delete
from sqlalchemy import distinct, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.evidence import ScrapedFieldEvidence
from app.models.gemini_call_log import GeminiCallLog
from app.models.scrape_run_summary import ScrapeRunSummary
from app.models.scrape_runtime import ScrapeRuntimeJob
from app.models.scraped_course import ScrapedCourse

log = logging.getLogger(__name__)

# The nine fields the spec wants explicit fill_rate columns for.
SUMMARY_FIELDS: tuple[str, ...] = (
    "international_fee",
    "ielts_overall",
    "pte_overall",
    "toefl_overall",
    "duration",
    "intake_months",
    "course_location",
    "study_mode",
    "cricos_code",
)


def _bucket_skip_reason(raw: str) -> str:
    """Map a free-form rejection_reason token to one of the seven spec buckets.

    Returns the column suffix (e.g. ``"domestic_only"``).
    """
    if not raw:
        return "other"
    r = raw.lower()
    if "domestic" in r:
        return "domestic_only"
    if "online" in r:
        return "online_only"
    if "international_fee" in r or "intl_fee" in r or "no_international" in r:
        return "no_international_fee"
    if "category_landing" in r or "landing_page" in r:
        return "category_landing_page"
    if "generic_category" in r or "category_page" in r:
        return "generic_category_page"
    if "fetch" in r or "timeout" in r or "http" in r:
        return "fetch_failed"
    return "other"


def bucket_skip_reasons(skip_reasons: Mapping[str, int]) -> dict[str, int]:
    """Aggregate the orchestrator's free-form skip_reasons dict into the
    seven spec buckets. Exposed for unit testing."""
    buckets = {
        "domestic_only": 0,
        "online_only": 0,
        "no_international_fee": 0,
        "category_landing_page": 0,
        "generic_category_page": 0,
        "fetch_failed": 0,
        "other": 0,
    }
    for reason, count in (skip_reasons or {}).items():
        buckets[_bucket_skip_reason(reason)] += int(count or 0)
    return buckets


async def compute_run_summary(
    db: AsyncSession,
    scrape_run_id: str,
    university_id: int,
    *,
    summary: Mapping[str, int] | None = None,
    skip_reasons: Mapping[str, int] | None = None,
) -> ScrapeRunSummary | None:
    """Compute and persist one ``scrape_run_summary`` row for a finished run.

    Args:
        db: open async session.
        scrape_run_id: ``scrape_runtime_jobs.runtime_job_id`` of the run.
        university_id: FK into ``universities``.
        summary: orchestrator's in-memory ``{discovered, staged, skipped,
            errors, fetch_failed}`` dict. Falls back to scrape_runtime_jobs
            counters if omitted.
        skip_reasons: orchestrator's free-form ``{token: count}`` dict.
            Empty dict if omitted.

    Returns the persisted row, or ``None`` if the runtime job wasn't found.
    """
    job_result = await db.execute(
        select(ScrapeRuntimeJob).where(ScrapeRuntimeJob.runtime_job_id == scrape_run_id)
    )
    job: ScrapeRuntimeJob | None = job_result.scalar_one_or_none()
    if job is None:
        log.warning("[RUN_SUMMARY] runtime job %s not found — skipping summary", scrape_run_id)
        return None

    started_at = job.started_at or datetime.now(timezone.utc)
    finished_at = job.completed_at or datetime.now(timezone.utc)

    # Defaults from the orchestrator's in-memory dict, falling back to the
    # persisted job counters when called outside the orchestrator (e.g.
    # tests or replay).
    summary = dict(summary or {})
    discovered = int(summary.get("discovered", job.total_found or 0))
    staged = int(summary.get("staged", job.imported or 0))
    skipped = int(summary.get("skipped", job.skipped or 0))
    fetch_errors = int(summary.get("fetch_failed", summary.get("errors", job.errors or 0)))

    skip_buckets = bucket_skip_reasons(skip_reasons or {})

    # Orchestrator counts fetch failures separately in ``summary["fetch_failed"]``
    # — they never enter ``skip_reasons``. Reconcile: the bucket should
    # reflect the true number of fetch failures from the orchestrator dict
    # if that value is larger (it almost always will be, since skip_reasons
    # currently has no fetch_failed token at all).
    skip_buckets["fetch_failed"] = max(skip_buckets["fetch_failed"], fetch_errors)

    # ── Per-field fill rates ──────────────────────────────────────────────
    # Denominator = staged courses for this run (status pending/review_ready).
    courses_total_result = await db.execute(
        select(func.count(ScrapedCourse.id))
        .where(ScrapedCourse.scrape_job_id == scrape_run_id)
        .where(ScrapedCourse.status.in_(["pending", "review_ready"]))
    )
    courses_total: int = courses_total_result.scalar() or 0

    fill_rates: dict[str, Decimal | None] = {f: None for f in SUMMARY_FIELDS}
    method_distribution: dict[str, dict[str, int]] = {}

    if courses_total > 0:
        # All evidence rows for staged courses in this run, grouped by
        # field+method. Same join pattern as compute_run_metrics. We
        # status-filter the join so the numerator can never include
        # rows from non-staged courses (would otherwise let fill-rate
        # drift if a course later moved to e.g. 'rejected').
        ev_result = await db.execute(
            select(
                ScrapedFieldEvidence.field_key,
                ScrapedFieldEvidence.extraction_method.label("method"),
                func.count(distinct(ScrapedFieldEvidence.scraped_course_id)).label("count"),
            )
            .join(ScrapedCourse, ScrapedCourse.id == ScrapedFieldEvidence.scraped_course_id)
            .where(ScrapedCourse.scrape_job_id == scrape_run_id)
            .where(ScrapedCourse.status.in_(["pending", "review_ready"]))
            .where(ScrapedFieldEvidence.selected.is_(True))
            .group_by(ScrapedFieldEvidence.field_key, ScrapedFieldEvidence.extraction_method)
        )

        per_field_total: dict[str, int] = {}
        for row in ev_result.all():
            field = row.field_key or ""
            method = row.method or "unknown"
            count = int(row.count or 0)
            method_distribution.setdefault(field, {})[method] = (
                method_distribution.get(field, {}).get(method, 0) + count
            )
            per_field_total[field] = per_field_total.get(field, 0) + count

        for field in SUMMARY_FIELDS:
            non_null = per_field_total.get(field, 0)
            # Cap at 1.0 — a single course can have multiple selected
            # evidence rows for the same field across methods, but we count
            # by distinct scraped_course_id above so the sum is fine.
            rate = min(non_null / courses_total, 1.0)
            fill_rates[field] = Decimal(f"{rate:.3f}")
            # Add a "null" bucket so the JSONB tells the full story.
            null_count = max(courses_total - non_null, 0)
            if field in method_distribution or null_count > 0:
                method_distribution.setdefault(field, {})["null"] = null_count

    # ── Cost ──────────────────────────────────────────────────────────────
    cost_result = await db.execute(
        select(
            func.count(GeminiCallLog.id).label("calls"),
            func.coalesce(func.sum(GeminiCallLog.cost_usd), 0.0).label("cost"),
        ).where(GeminiCallLog.scrape_run_id == scrape_run_id)
    )
    cost_row = cost_result.one()
    gemini_calls = int(cost_row.calls or 0)
    gemini_cost_usd = Decimal(str(cost_row.cost or 0)).quantize(Decimal("0.000001"))

    # ── Persist ───────────────────────────────────────────────────────────
    # Idempotent: drop any prior row for this run.
    await db.execute(
        sa_delete(ScrapeRunSummary).where(ScrapeRunSummary.scrape_run_id == scrape_run_id)
    )

    row = ScrapeRunSummary(
        scrape_run_id=scrape_run_id,
        university_id=university_id,
        run_started_at=started_at,
        run_finished_at=finished_at,
        candidates_discovered=discovered,
        candidates_staged=staged,
        candidates_skipped=skipped,
        skipped_domestic_only=skip_buckets["domestic_only"],
        skipped_online_only=skip_buckets["online_only"],
        skipped_no_international_fee=skip_buckets["no_international_fee"],
        skipped_category_landing_page=skip_buckets["category_landing_page"],
        skipped_generic_category_page=skip_buckets["generic_category_page"],
        skipped_fetch_failed=skip_buckets["fetch_failed"],
        skipped_other=skip_buckets["other"],
        fill_rate_international_fee=fill_rates["international_fee"],
        fill_rate_ielts_overall=fill_rates["ielts_overall"],
        fill_rate_pte_overall=fill_rates["pte_overall"],
        fill_rate_toefl_overall=fill_rates["toefl_overall"],
        fill_rate_duration=fill_rates["duration"],
        fill_rate_intake_months=fill_rates["intake_months"],
        fill_rate_course_location=fill_rates["course_location"],
        fill_rate_study_mode=fill_rates["study_mode"],
        fill_rate_cricos_code=fill_rates["cricos_code"],
        method_distribution=method_distribution or None,
        gemini_calls=gemini_calls,
        gemini_cost_usd=gemini_cost_usd,
        fetch_errors=fetch_errors,
    )
    db.add(row)
    await db.commit()

    log.info(
        "[RUN_SUMMARY] run %s uni %s: staged=%d skipped=%d gemini_calls=%d "
        "gemini_cost=$%.4f fields_with_evidence=%d",
        scrape_run_id, university_id, staged, skipped,
        gemini_calls, float(gemini_cost_usd), len(method_distribution),
    )
    return row
