"""Priority 5 — Component 2: baseline seeding and weekly refresh.

``seed_baselines`` computes per-university, per-field expected fill rates
from the last 30 days of completed scrape runs and upserts them into
``university_field_baselines``.

  * Uses median fill rate as the expected value.
  * Uses p10 − 5pp (never below 0.50) as the alert floor.
  * Only considers runs with status='completed' (no flagged_bad column yet).
  * Universities with < 3 historical runs are skipped — the alert evaluator
    falls back to GLOBAL_DEFAULT_FLOORS for those.

Can be run as a one-shot script:

    cd backend-py
    PYTHONPATH=. python -m app.scripts.seed_baselines

Or triggered by the weekly Celery beat task (scrape.refresh_baselines).
"""
from __future__ import annotations

import asyncio
import json
import logging
import statistics
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.scrape_run_metrics import ScrapeRunMetrics
from app.models.scrape_runtime import ScrapeRuntimeJob
from app.models.university_field_baseline import UniversityFieldBaseline

log = logging.getLogger(__name__)

_MIN_RUNS_FOR_BASELINE = 3
_TRAILING_DAYS = 30
_MIN_FLOOR = 0.50


async def seed_baselines(db: AsyncSession) -> int:
    """Upsert baselines from the trailing 30 days.  Returns number of rows upserted."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=_TRAILING_DAYS)

    # Fetch IDs of eligible completed runs in the window
    eligible_jobs_result = await db.execute(
        select(ScrapeRuntimeJob.runtime_job_id, ScrapeRuntimeJob.university_id)
        .where(ScrapeRuntimeJob.status == "completed")
        .where(ScrapeRuntimeJob.completed_at >= cutoff)
        .where(ScrapeRuntimeJob.university_id.isnot(None))
    )
    eligible_jobs = eligible_jobs_result.all()

    if not eligible_jobs:
        log.info("[SEED BASELINES] No eligible completed runs in the last %d days", _TRAILING_DAYS)
        return 0

    # Group runs by university
    uni_runs: dict[int, list[str]] = {}
    for job_id, uni_id in eligible_jobs:
        if uni_id is not None:
            uni_runs.setdefault(uni_id, []).append(job_id)

    upserted = 0

    for uni_id, run_ids in uni_runs.items():
        if len(run_ids) < _MIN_RUNS_FOR_BASELINE:
            log.debug(
                "[SEED BASELINES] uni %d: only %d runs — skipping (need %d)",
                uni_id, len(run_ids), _MIN_RUNS_FOR_BASELINE,
            )
            continue

        # Load all metrics for this university's eligible runs
        metrics_result = await db.execute(
            select(ScrapeRunMetrics)
            .where(ScrapeRunMetrics.scrape_run_id.in_(run_ids))
        )
        all_metrics = list(metrics_result.scalars().all())

        if not all_metrics:
            continue

        # Group by (field_key) → per-run aggregated fill rate
        # (same aggregation as alerts._aggregate_by_field)
        from app.services.scraper.alerts import _aggregate_by_field

        per_run_field_rates: dict[str, list[float]] = {}
        for run_id in run_ids:
            run_m = [m for m in all_metrics if m.scrape_run_id == run_id]
            if not run_m:
                continue
            field_totals = _aggregate_by_field(run_m)
            for fk, rate in field_totals.items():
                per_run_field_rates.setdefault(fk, []).append(rate)

        # Per-method distribution across all runs for this university
        method_dist_by_field: dict[str, dict[str, list[float]]] = {}
        for m in all_metrics:
            if m.courses_total == 0:
                continue
            method_dist_by_field.setdefault(m.field_key, {}).setdefault(m.method, []).append(
                m.count / m.courses_total
            )

        for field_key, rates in per_run_field_rates.items():
            if len(rates) < _MIN_RUNS_FOR_BASELINE:
                continue

            median_rate = statistics.median(rates)
            p10_rate = sorted(rates)[max(0, int(len(rates) * 0.10) - 1)]
            floor = max(_MIN_FLOOR, round(p10_rate - 0.05, 4))

            # Method distribution: median share per method
            method_dist: dict[str, float] = {}
            for method, method_rates in method_dist_by_field.get(field_key, {}).items():
                method_dist[method] = round(statistics.median(method_rates), 4)

            # Upsert via raw SQL (PostgreSQL ON CONFLICT DO UPDATE)
            await db.execute(
                text("""
                    INSERT INTO university_field_baselines
                        (university_id, field_key, expected_fill_rate,
                         expected_method_distribution, floor_threshold,
                         sample_size, last_updated)
                    VALUES
                        (:uni_id, :field_key, :expected_fill_rate,
                         :method_dist::jsonb, :floor_threshold,
                         :sample_size, NOW())
                    ON CONFLICT (university_id, field_key) DO UPDATE SET
                        expected_fill_rate          = EXCLUDED.expected_fill_rate,
                        expected_method_distribution = EXCLUDED.expected_method_distribution,
                        floor_threshold             = EXCLUDED.floor_threshold,
                        sample_size                 = EXCLUDED.sample_size,
                        last_updated                = NOW()
                """),
                {
                    "uni_id": uni_id,
                    "field_key": field_key,
                    "expected_fill_rate": round(median_rate, 4),
                    "method_dist": json.dumps(method_dist),
                    "floor_threshold": floor,
                    "sample_size": len(rates),
                },
            )
            upserted += 1

    await db.commit()
    log.info(
        "[SEED BASELINES] Done: upserted %d baseline rows across %d universities",
        upserted,
        len([u for u in uni_runs if len(uni_runs[u]) >= _MIN_RUNS_FOR_BASELINE]),
    )
    return upserted


# ---------------------------------------------------------------------------
# Standalone runner
# ---------------------------------------------------------------------------

async def _main() -> None:
    import os
    from app.database import AsyncSessionLocal, engine
    await engine.dispose()
    async with AsyncSessionLocal() as db:
        count = await seed_baselines(db)
        print(f"Upserted {count} baseline rows")


if __name__ == "__main__":
    asyncio.run(_main())
