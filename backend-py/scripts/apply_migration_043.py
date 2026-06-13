"""Migration 043 — add agent_recovery_results table.

This table stores per-field recovery results produced by the Agent Recovery
post-pass that runs after every scrape job.  Operators use the review UI to
apply or reject each result; applied results are written back into
scraped_courses and create an evidence row in scraped_field_evidence.

Run once per environment:
  PYTHONPATH=backend-py python3 backend-py/scripts/apply_migration_043.py
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
    CREATE TABLE IF NOT EXISTS agent_recovery_results (
        id               SERIAL PRIMARY KEY,
        scraped_course_id INTEGER NOT NULL
                         REFERENCES scraped_courses(id) ON DELETE CASCADE,
        scrape_run_id    TEXT NOT NULL,
        field            TEXT NOT NULL,
        recovered_value  TEXT,
        source_url       TEXT,
        source_type      TEXT,
        evidence_text    TEXT,
        confidence       DOUBLE PRECISION,
        mapping_reason   TEXT,
        status           TEXT NOT NULL DEFAULT 'pending',
        created_at       TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_agent_recovery_course_field_status
        ON agent_recovery_results (scraped_course_id, field, status)
        WHERE status = 'pending'
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_agent_recovery_scrape_run
        ON agent_recovery_results (scrape_run_id)
    """,
]


async def main() -> None:
    engine = create_async_engine(settings.database_url, echo=False)
    async with engine.begin() as conn:
        for stmt in STATEMENTS:
            await conn.execute(text(stmt))
    await engine.dispose()
    log.info("Migration 043 applied — agent_recovery_results table created")


if __name__ == "__main__":
    asyncio.run(main())
