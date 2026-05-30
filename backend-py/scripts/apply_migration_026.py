#!/usr/bin/env python3
"""Migration 026 — Confidence Trend: avg_verification_confidence on scrape_runtime_jobs.

Adds column:
  - scrape_runtime_jobs.avg_verification_confidence  FLOAT nullable
    Written at job completion with the mean of scraped_courses.avg_verification_confidence
    for all courses staged in that run. Used by the Confidence Trend API to show
    per-job confidence history and trend direction (improving / declining / stable).

Apply on production:
    cd /root/University-and-Course-data
    PYTHONPATH=backend-py python3 backend-py/scripts/apply_migration_026.py
"""
import asyncio
import os

_raw_url = os.environ.get(
    "DATABASE_URL",
    "postgresql+asyncpg://postgres:password@127.0.0.1:5432/university_portal",
)
DATABASE_URL = _raw_url.replace("postgresql://", "postgresql+asyncpg://", 1)

_STMTS = [
    """
    ALTER TABLE scrape_runtime_jobs
        ADD COLUMN IF NOT EXISTS avg_verification_confidence FLOAT
    """,
    """
    COMMENT ON COLUMN scrape_runtime_jobs.avg_verification_confidence IS
        'Phase 9 Confidence Trend — mean of scraped_courses.avg_verification_confidence '
        'for all courses staged in this run (0-100). NULL when no courses were verified.'
    """,
    "CREATE INDEX IF NOT EXISTS idx_srj_uni_conf ON scrape_runtime_jobs (university_id, completed_at DESC) WHERE avg_verification_confidence IS NOT NULL",
]


async def run() -> None:
    from app.database import engine
    from sqlalchemy import text

    async with engine.begin() as conn:
        for stmt in _STMTS:
            await conn.execute(text(stmt))
    print("Migration 026 applied: scrape_runtime_jobs.avg_verification_confidence.")


if __name__ == "__main__":
    asyncio.run(run())
