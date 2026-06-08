"""Migration 041 — create page_snapshots table.

Apply on prod:
  cd /root/University-and-Course-data && PYTHONPATH=backend-py python3 backend-py/scripts/apply_migration_041.py

Apply on dev:
  PYTHONPATH=backend-py python3 backend-py/scripts/apply_migration_041.py
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
    CREATE TABLE IF NOT EXISTS page_snapshots (
        id              BIGSERIAL PRIMARY KEY,
        university_id   INTEGER NOT NULL REFERENCES universities(id) ON DELETE CASCADE,
        scrape_job_id   TEXT    NOT NULL REFERENCES scrape_runtime_jobs(runtime_job_id) ON DELETE CASCADE,
        course_url      TEXT    NOT NULL,
        url_hash        TEXT    NOT NULL,
        snapshot_type   TEXT    NOT NULL DEFAULT 'html',
        storage_path    TEXT,
        status_code     INTEGER,
        content_length  INTEGER,
        fetch_method    TEXT,
        extractor_version TEXT,
        fetched_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_page_snapshots_job ON page_snapshots (scrape_job_id)",
    "CREATE INDEX IF NOT EXISTS ix_page_snapshots_uni_url ON page_snapshots (university_id, url_hash)",
    "CREATE INDEX IF NOT EXISTS ix_page_snapshots_url_hash ON page_snapshots (url_hash)",
    "COMMENT ON TABLE page_snapshots IS 'Index of HTML/JSON/PDF snapshots saved to S3 during scrape jobs. Actual content lives in S3; this table is metadata only.'",
]


async def main() -> None:
    engine = create_async_engine(settings.database_url, echo=False)
    async with engine.begin() as conn:
        for stmt in STATEMENTS:
            await conn.execute(text(stmt))
    await engine.dispose()
    log.info("Migration 041 applied — page_snapshots table ready")


if __name__ == "__main__":
    asyncio.run(main())
