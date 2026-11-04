"""Migration 046 — create discovery_url_cache table (fetch-layer brief, C1).

Caches the raw discovered course-link list per university for 7 days so a
re-scrape within that window skips the entire discovery phase (BFS crawl,
sitemap probes, browser rendering, Wayback sweeps) and goes straight to
extraction.  Read/write policy lives in the orchestrator; see
app/models/discovery_url_cache.py for details.

Run once per environment:
  PYTHONPATH=backend-py python3 backend-py/scripts/apply_migration_046.py

Production:
  cd /root/University-and-Course-data && PYTHONPATH=backend-py python3 backend-py/scripts/apply_migration_046.py
"""
from __future__ import annotations

import asyncio
import logging

from sqlalchemy import text
from app.legacy_migration_database import create_migration_engine


logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS discovery_url_cache (
        university_id INTEGER PRIMARY KEY,
        links JSONB NOT NULL,
        link_count INTEGER NOT NULL DEFAULT 0,
        discovered_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
]


async def main() -> None:
    engine = create_migration_engine(echo=False)
    async with engine.begin() as conn:
        for stmt in STATEMENTS:
            await conn.execute(text(stmt))
    await engine.dispose()
    log.info("Migration 046 applied — discovery_url_cache table created")


if __name__ == "__main__":
    asyncio.run(main())
