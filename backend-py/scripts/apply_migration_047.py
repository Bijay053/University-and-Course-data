"""Migration 047 — persist compact AI repair audit evidence."""
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
    CREATE TABLE IF NOT EXISTS ai_repair_audits (
        session_id TEXT PRIMARY KEY,
        scrape_job_id TEXT NOT NULL REFERENCES scrape_runtime_jobs(runtime_job_id) ON DELETE CASCADE,
        university_id INTEGER NOT NULL REFERENCES universities(id) ON DELETE CASCADE,
        status TEXT NOT NULL,
        evidence JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_ai_repair_audits_job_updated ON ai_repair_audits(scrape_job_id, updated_at DESC)",
    "CREATE INDEX IF NOT EXISTS ix_ai_repair_audits_university ON ai_repair_audits(university_id)",
    """
    COMMENT ON COLUMN ai_repair_audits.evidence IS
    'Compact decisions and before/after values; HTML remains in page_snapshots/S3 and is referenced by ID'
    """,
]


async def main() -> None:
    engine = create_async_engine(settings.database_url, echo=False)
    async with engine.begin() as conn:
        for stmt in STATEMENTS:
            await conn.execute(text(stmt))
    await engine.dispose()
    log.info("Migration 047 applied — ai_repair_audits table created")


if __name__ == "__main__":
    asyncio.run(main())