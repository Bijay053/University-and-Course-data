"""Celery task: daily university health snapshot.

Reads v_university_health and upserts one row per university into
university_health_snapshots. Idempotent — re-running on the same day
overwrites the existing row (ON CONFLICT DO UPDATE).

Beat schedule: 01:30 UTC daily (after scrapes finish, before the 02:00
nightly regression sweep so the sweep can reference fresh snapshots).
"""

import asyncio
import logging

from sqlalchemy import text

from app.database import AsyncSessionLocal

logger = logging.getLogger(__name__)


def snapshot_health_daily() -> dict:
    """Synchronous entry point called by Celery."""
    return asyncio.run(_run())


async def _run() -> dict:
    async with AsyncSessionLocal() as db:
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

        rows = result.rowcount
        logger.info("health_snapshot: upserted %d rows for %s", rows, "CURRENT_DATE")
        return {"upserted": rows}
