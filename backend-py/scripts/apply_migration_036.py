"""Migration 036 — Create v_university_health view.

Adds:
  - v_university_health: per-university scraping health metrics
    (discovery, extraction, fee, english, intake coverage + overall score)

Apply on dev:
    PYTHONPATH=backend-py python3 backend-py/scripts/apply_migration_036.py

Apply on prod:
    cd /root/University-and-Course-data && PYTHONPATH=backend-py python3 backend-py/scripts/apply_migration_036.py
"""

import asyncio
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sqlalchemy import text
from app.database import AsyncSessionLocal


DROP_DDL = "DROP VIEW IF EXISTS v_university_health CASCADE"

VIEW_DDL = """
CREATE VIEW v_university_health AS
WITH latest_job AS (
    -- Most recent scrape job per university (any status — we want import count)
    SELECT DISTINCT ON (university_id)
        university_id,
        imported,
        total_found,
        started_at
    FROM scrape_runtime_jobs
    WHERE university_id IS NOT NULL
    ORDER BY university_id, started_at DESC
),
course_stats AS (
    -- Aggregate per-university metrics across all non-rejected staged courses
    SELECT
        university_id,
        COUNT(*)::int                                                               AS total_courses,
        COALESCE(ROUND(AVG(completeness))::int, 0)                                 AS extraction_health,
        COALESCE(ROUND(
            100.0 * COUNT(*) FILTER (WHERE international_fee IS NOT NULL)
            / NULLIF(COUNT(*), 0)
        )::int, 0)                                                                  AS fee_coverage,
        COALESCE(ROUND(
            100.0 * COUNT(*) FILTER (
                WHERE ielts_overall   IS NOT NULL
                   OR pte_overall     IS NOT NULL
                   OR toefl_overall   IS NOT NULL
            ) / NULLIF(COUNT(*), 0)
        )::int, 0)                                                                  AS english_coverage,
        COALESCE(ROUND(
            100.0 * COUNT(*) FILTER (
                WHERE intake_months IS NOT NULL
                  AND jsonb_array_length(intake_months) > 0
            ) / NULLIF(COUNT(*), 0)
        )::int, 0)                                                                  AS intake_coverage
    FROM scraped_courses
    WHERE status != 'rejected'
    GROUP BY university_id
)
SELECT
    cs.university_id,
    cs.total_courses,
    lj.imported           AS last_imported,
    lj.total_found        AS last_total_found,
    lj.started_at         AS last_job_at,
    -- Discovery: 20+ staged courses from the last job = 100%.
    -- Falls back to staged course count when no job record exists.
    LEAST(100, COALESCE(lj.imported, cs.total_courses) * 5)::int    AS discovery_health,
    cs.extraction_health,
    cs.fee_coverage,
    cs.english_coverage,
    cs.intake_coverage,
    -- Weighted overall: discovery 20%, extraction 30%, fee 20%, english 15%, intake 15%
    ROUND(
        0.20 * LEAST(100.0, COALESCE(lj.imported, cs.total_courses) * 5.0) +
        0.30 * cs.extraction_health +
        0.20 * cs.fee_coverage +
        0.15 * cs.english_coverage +
        0.15 * cs.intake_coverage
    )::int                                                            AS overall_health
FROM course_stats cs
LEFT JOIN latest_job lj USING (university_id)
"""


async def run() -> None:
    async with AsyncSessionLocal() as db:
        await db.execute(text(DROP_DDL))
        await db.execute(text(VIEW_DDL))
        await db.commit()
        print("Migration 036 applied — v_university_health view created/replaced.")

        # Quick smoke-test
        result = await db.execute(text("SELECT COUNT(*) FROM v_university_health"))
        count = result.scalar()
        print(f"  Rows in view: {count}")


if __name__ == "__main__":
    asyncio.run(run())
