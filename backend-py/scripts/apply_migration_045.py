"""Migration 045 — add gate_skip_counts JSONB column to scrape_runtime_jobs.

Records per-run counts for the five latency-gate skip paths introduced in
Tasks #233, #235, and #236:
  - gemini_timeout       (Task #233 gate 1 — SDK call exceeded timeout)
  - gemini_circuit_open  (Task #233 gate 1 — circuit breaker tripped)
  - vision_early_exit    (Task #233 gate 2 — all English overalls already filled)
  - browser_http_skipped (Task #233 gate 3 — confirmed browser-only host)
  - challenge_shell      (Task #236 gate  — browser returned CF/Imperva interstitial)

Column is nullable; NULL means the run pre-dates Task #235 or the counters
were never initialised (e.g. direct call to extract_course in tests).

Run once per environment:
  PYTHONPATH=backend-py python3 backend-py/scripts/apply_migration_045.py

Production (no asyncpg DNS issue since we use IP literal from settings):
  cd /root/University-and-Course-data && PYTHONPATH=backend-py python3 backend-py/scripts/apply_migration_045.py
"""
from __future__ import annotations

import asyncio
import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import settings

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

STATEMENTS = [
    """
    ALTER TABLE scrape_runtime_jobs
        ADD COLUMN IF NOT EXISTS gate_skip_counts JSONB
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_scrape_runtime_jobs_gate_skip_counts
        ON scrape_runtime_jobs USING gin (gate_skip_counts)
        WHERE gate_skip_counts IS NOT NULL
    """,
]


async def main() -> None:
    engine = create_async_engine(settings.database_url, echo=False)
    async with engine.begin() as conn:
        for stmt in STATEMENTS:
            await conn.execute(text(stmt))
    await engine.dispose()
    log.info(
        "Migration 045 applied — gate_skip_counts JSONB column added to scrape_runtime_jobs"
    )


if __name__ == "__main__":
    asyncio.run(main())
