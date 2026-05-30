#!/usr/bin/env python3
"""Migration 023 — Create scraper_patterns table (Phase 3: Autonomous Learning).

This table stores successful per-field extraction rules keyed by platform type
(e.g. "wordpress", "drupal", "searchstax", "sitemap_first") so future
universities on the same platform start with proven rules instead of zero.

Apply on production:
    cd /root/University-and-Course-data
    PYTHONPATH=backend-py python3 backend-py/scripts/apply_migration_023.py
"""
import asyncio
import os

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql+asyncpg://postgres:password@127.0.0.1:5432/university_portal",
)

_STMTS = [
    """
    CREATE TABLE IF NOT EXISTS scraper_patterns (
        id               SERIAL  PRIMARY KEY,
        platform_type    TEXT    NOT NULL,
        field_key        TEXT    NOT NULL,
        rules_json       JSONB   NOT NULL,
        success_count    INTEGER NOT NULL DEFAULT 1,
        avg_fill_rate    FLOAT   NOT NULL DEFAULT 0.0,
        last_promoted_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT now(),
        created_at       TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT now(),
        UNIQUE (platform_type, field_key)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_scraper_patterns_platform
        ON scraper_patterns (platform_type)
    """,
    """
    COMMENT ON TABLE scraper_patterns IS
        'Phase 3 autonomous learning: per-(platform, field) extraction rules '
        'promoted from successful scrapes so new universities start with proven rules.'
    """,
]


async def run() -> None:
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy import text

    engine = create_async_engine(DATABASE_URL, echo=False)
    async with engine.begin() as conn:
        for stmt in _STMTS:
            await conn.execute(text(stmt.strip()))
    await engine.dispose()
    print("Migration 023 applied: scraper_patterns table ready.")


if __name__ == "__main__":
    asyncio.run(run())
