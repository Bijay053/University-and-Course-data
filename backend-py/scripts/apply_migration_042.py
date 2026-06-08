"""Migration 042 — extend page_snapshots with version metadata + original extraction.

New columns:
  yaml_version        TEXT    — SHA-256 (8 chars) of the uni YAML used during extraction
  scraper_commit      TEXT    — git short SHA of the scraper at extraction time
  original_extraction JSONB   — extracted field values from the original scrape run
                                 (used as the left side of replay diffs)

Run once per environment:
  PYTHONPATH=backend-py python3 backend-py/scripts/apply_migration_042.py
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
    "ALTER TABLE page_snapshots ADD COLUMN IF NOT EXISTS yaml_version TEXT",
    "ALTER TABLE page_snapshots ADD COLUMN IF NOT EXISTS scraper_commit TEXT",
    "ALTER TABLE page_snapshots ADD COLUMN IF NOT EXISTS original_extraction JSONB",
    (
        "COMMENT ON COLUMN page_snapshots.original_extraction IS "
        "'Extracted field values from the original scrape run — used as the "
        "baseline (left side) when replaying extractors against saved HTML/JSON.'"
    ),
]


async def main() -> None:
    engine = create_async_engine(settings.database_url, echo=False)
    async with engine.begin() as conn:
        for stmt in STATEMENTS:
            await conn.execute(text(stmt))
    await engine.dispose()
    log.info("Migration 042 applied — yaml_version, scraper_commit, original_extraction added")


if __name__ == "__main__":
    asyncio.run(main())
