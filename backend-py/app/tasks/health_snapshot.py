"""Celery task: daily university health snapshot + regression detection.

Beat schedule: 01:30 UTC daily (after overnight scrapes, before 02:00 regression sweep).

Steps:
  1. Upsert v_university_health → university_health_snapshots (idempotent per day).
  2. Run regression detector — compares current vs previous snapshot and
     writes any new alerts to university_regression_alerts.
"""

from __future__ import annotations

import asyncio
import logging

from sqlalchemy import text

from app.database import AsyncSessionLocal, engine
from app.tasks.celery_app import celery_app

log = logging.getLogger(__name__)


def _sync_dispose() -> None:
    try:
        engine.sync_engine.dispose(close=False)
    except Exception as exc:  # noqa: BLE001
        log.warning("health_snapshot _sync_dispose: %s", exc)


async def _run() -> dict:
    from app.services.regression_detector import run_regression_detection

    async with AsyncSessionLocal() as db:
        # Step 1 — snapshot current health scores
        result = await db.execute(text("""
            INSERT INTO university_health_snapshots
                (university_id, snapshot_date, overall_health, discovery_health,
                 extraction_health, fee_coverage, english_coverage,
                 intake_coverage, total_courses, created_at)
            SELECT
                university_id,
                CURRENT_DATE,
                overall_health,
                discovery_health,
                extraction_health,
                fee_coverage,
                english_coverage,
                intake_coverage,
                total_courses,
                NOW()
            FROM v_university_health
            ON CONFLICT (university_id, snapshot_date) DO UPDATE SET
                overall_health    = EXCLUDED.overall_health,
                discovery_health  = EXCLUDED.discovery_health,
                extraction_health = EXCLUDED.extraction_health,
                fee_coverage      = EXCLUDED.fee_coverage,
                english_coverage  = EXCLUDED.english_coverage,
                intake_coverage   = EXCLUDED.intake_coverage,
                total_courses     = EXCLUDED.total_courses,
                created_at        = NOW()
        """))
        await db.commit()
        upserted = result.rowcount
        log.info("health_snapshot: upserted %d rows", upserted)

        # Step 2 — regression detection
        regression_result = await run_regression_detection(db)

    # Step 3 — trigger auto-repair for any universities with new alerts
    affected = regression_result.get("affected_university_ids", [])
    if affected:
        for uid in affected:
            try:
                celery_app.send_task(
                    "auto_repair.generate_suggestion",
                    args=[uid, None],
                    queue="scrape",
                )
                log.info("health_snapshot: queued auto_repair for uni %d", uid)
            except Exception as exc:  # noqa: BLE001
                log.warning("health_snapshot: could not queue auto_repair for uni %d: %s", uid, exc)

    return {"upserted": upserted, **regression_result}


@celery_app.task(name="health.snapshot_daily", bind=True, max_retries=0)
def snapshot_health_daily(self) -> dict:  # noqa: ANN001
    """Snapshot university health scores and detect regressions."""
    log.info("health.snapshot_daily: starting")
    _sync_dispose()
    try:
        result = asyncio.run(_run())
        log.info("health.snapshot_daily: %s", result)
        return result
    except Exception as exc:  # noqa: BLE001
        log.error("health.snapshot_daily: failed: %s", exc)
        return {"error": str(exc)}
