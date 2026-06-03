"""Migration 037 — University health history + improved discovery formula.

Adds:
  - university_health_snapshots table (daily per-uni health history)
  - Replaces v_university_health with improved discovery formula:
      · ≥3 historical jobs → 100.0 * imported / median_historical_imported
      · fallback (new uni, <3 jobs) → imported * 5 (20 courses = 100%)

Apply on dev:
    PYTHONPATH=backend-py python3 backend-py/scripts/apply_migration_037.py

Apply on prod:
    cd /root/University-and-Course-data && PYTHONPATH=backend-py python3 backend-py/scripts/apply_migration_037.py
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sqlalchemy import text
from app.database import AsyncSessionLocal


SNAPSHOTS_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS university_health_snapshots (
    id               SERIAL PRIMARY KEY,
    university_id    INTEGER      NOT NULL,
    snapshot_date    DATE         NOT NULL DEFAULT CURRENT_DATE,
    overall_health   INTEGER      NOT NULL,
    discovery_health INTEGER,
    extraction_health INTEGER,
    fee_coverage     INTEGER,
    english_coverage INTEGER,
    intake_coverage  INTEGER,
    total_courses    INTEGER,
    created_at       TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    UNIQUE (university_id, snapshot_date)
)
"""

SNAPSHOTS_INDEX_DDL = """
CREATE INDEX IF NOT EXISTS idx_uhs_uni_date
    ON university_health_snapshots (university_id, snapshot_date DESC)
"""

DROP_VIEW = "DROP VIEW IF EXISTS v_university_health CASCADE"

VIEW_DDL = """
CREATE VIEW v_university_health AS
WITH latest_job AS (
    -- Most recent completed scrape job per university
    SELECT DISTINCT ON (university_id)
        university_id,
        imported,
        total_found,
        started_at
    FROM scrape_runtime_jobs
    WHERE university_id IS NOT NULL
    ORDER BY university_id, started_at DESC
),
historical_baselines AS (
    -- Median imported count from last 60 days, one row per calendar day
    -- (uses the latest run per day to avoid double-counting multi-run days).
    -- Requires >=3 qualifying days to be considered reliable.
    SELECT
        university_id,
        PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY daily_imported)::int AS median_imported
    FROM (
        SELECT DISTINCT ON (university_id, DATE_TRUNC('day', started_at))
            university_id,
            COALESCE(imported, 0) AS daily_imported,
            started_at
        FROM scrape_runtime_jobs
        WHERE university_id IS NOT NULL
          AND started_at > NOW() - INTERVAL '60 days'
        ORDER BY university_id, DATE_TRUNC('day', started_at), started_at DESC
    ) daily
    WHERE daily_imported > 0
    GROUP BY university_id
    HAVING COUNT(*) >= 3
),
course_stats AS (
    SELECT
        university_id,
        COUNT(*)::int                                                                AS total_courses,
        COALESCE(ROUND(AVG(completeness))::int, 0)                                  AS extraction_health,
        COALESCE(ROUND(
            100.0 * COUNT(*) FILTER (WHERE international_fee IS NOT NULL)
            / NULLIF(COUNT(*), 0)
        )::int, 0)                                                                   AS fee_coverage,
        COALESCE(ROUND(
            100.0 * COUNT(*) FILTER (
                WHERE ielts_overall IS NOT NULL
                   OR pte_overall   IS NOT NULL
                   OR toefl_overall IS NOT NULL
            ) / NULLIF(COUNT(*), 0)
        )::int, 0)                                                                   AS english_coverage,
        COALESCE(ROUND(
            100.0 * COUNT(*) FILTER (
                WHERE intake_months IS NOT NULL
                  AND jsonb_array_length(intake_months) > 0
            ) / NULLIF(COUNT(*), 0)
        )::int, 0)                                                                   AS intake_coverage
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
    -- Discovery: use historical median when >=3 qualifying days exist.
    -- Below median = proportional score; above = 100%.
    -- Fallback for new/low-history universities: 20 courses staged = 100%.
    CASE
        WHEN lj.imported IS NULL
            THEN LEAST(100, COALESCE(cs.total_courses, 0) * 5)::int
        WHEN hb.median_imported IS NOT NULL AND hb.median_imported > 0
            THEN LEAST(100, ROUND(100.0 * lj.imported / hb.median_imported))::int
        ELSE LEAST(100, lj.imported * 5)::int
    END                                                                              AS discovery_health,
    cs.extraction_health,
    cs.fee_coverage,
    cs.english_coverage,
    cs.intake_coverage,
    -- Weighted overall: discovery 20%, extraction 30%, fee 20%, english 15%, intake 15%
    ROUND(
        0.20 * CASE
                   WHEN lj.imported IS NULL
                       THEN LEAST(100.0, COALESCE(cs.total_courses, 0) * 5.0)
                   WHEN hb.median_imported IS NOT NULL AND hb.median_imported > 0
                       THEN LEAST(100.0, ROUND(100.0 * lj.imported / hb.median_imported))
                   ELSE LEAST(100.0, lj.imported * 5.0)
               END +
        0.30 * cs.extraction_health +
        0.20 * cs.fee_coverage +
        0.15 * cs.english_coverage +
        0.15 * cs.intake_coverage
    )::int                                                                           AS overall_health
FROM course_stats cs
LEFT JOIN latest_job          lj USING (university_id)
LEFT JOIN historical_baselines hb USING (university_id)
"""


async def run() -> None:
    async with AsyncSessionLocal() as db:
        await db.execute(text(SNAPSHOTS_TABLE_DDL))
        await db.execute(text(SNAPSHOTS_INDEX_DDL))
        await db.execute(text(DROP_VIEW))
        await db.execute(text(VIEW_DDL))
        await db.commit()

        print("Migration 037 applied.")

        r = await db.execute(text("SELECT COUNT(*) FROM university_health_snapshots"))
        print(f"  university_health_snapshots rows: {r.scalar()}")

        r = await db.execute(text("SELECT COUNT(*) FROM v_university_health"))
        print(f"  v_university_health rows: {r.scalar()}")

        # Spot check
        r = await db.execute(text(
            "SELECT university_id, discovery_health, overall_health "
            "FROM v_university_health ORDER BY overall_health DESC LIMIT 5"
        ))
        print("  Top 5 by overall health:")
        for row in r:
            print(f"    uni={row[0]}  disc={row[1]}  overall={row[2]}")


if __name__ == "__main__":
    asyncio.run(run())
