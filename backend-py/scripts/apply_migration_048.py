"""Migration 048 — record the worker release for every scrape job claim."""
from __future__ import annotations

import asyncio
import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import settings

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

STATEMENTS = [
    "ALTER TABLE scrape_runtime_jobs ADD COLUMN IF NOT EXISTS release_revision TEXT",
    "ALTER TABLE scrape_runtime_jobs ADD COLUMN IF NOT EXISTS release_history JSONB",
    """
    COMMENT ON COLUMN scrape_runtime_jobs.release_revision IS
    'Release revision used by the most recent worker that successfully claimed this job'
    """,
    """
    COMMENT ON COLUMN scrape_runtime_jobs.release_history IS
    'Append-only worker claim history with release revision and claim timestamp'
    """,
]


async def main() -> None:
    engine = create_async_engine(settings.database_url, echo=False)
    async with engine.begin() as conn:
        for stmt in STATEMENTS:
            await conn.execute(text(stmt))
    await engine.dispose()
    log.info("Migration 048 applied — scrape worker release history added")


if __name__ == "__main__":
    asyncio.run(main())