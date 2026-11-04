"""Migration 032 — university_watchers table (Phase 13: Autonomous Monitoring Engine).

Apply on production:
    cd /root/University-and-Course-data
    sudo -u postgres psql -d university_portal -f backend-py/scripts/migration_032.sql

Or via Python (uses managed configuration and verified TLS):
    PYTHONPATH=backend-py python3 backend-py/scripts/apply_migration_032.py
"""
from __future__ import annotations

import asyncio

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from app.legacy_migration_database import create_migration_engine

SQL = """
CREATE TABLE IF NOT EXISTS university_watchers (
    id                      SERIAL PRIMARY KEY,
    university_id           INTEGER REFERENCES universities(id) ON DELETE CASCADE,
    enabled                 BOOLEAN NOT NULL DEFAULT TRUE,
    monitoring_strategy     TEXT    NOT NULL DEFAULT 'passive',
    probe_url               TEXT,
    etag                    TEXT,
    page_hash               TEXT,
    sitemap_hash            TEXT,
    last_probe_result       TEXT,
    last_probe_status_code  INTEGER,
    last_probe_error        TEXT,
    consecutive_unchanged   INTEGER NOT NULL DEFAULT 0,
    total_checks            INTEGER NOT NULL DEFAULT 0,
    total_changes_detected  INTEGER NOT NULL DEFAULT 0,
    total_scrapes_triggered INTEGER NOT NULL DEFAULT 0,
    change_frequency_days   FLOAT,
    most_changed_pages      JSONB,
    most_stable_pages       JSONB,
    last_checked_at         TIMESTAMPTZ,
    last_changed_at         TIMESTAMPTZ,
    last_triggered_at       TIMESTAMPTZ,
    next_check_at           TIMESTAMPTZ,
    last_scrape_job_id      TEXT REFERENCES scrape_runtime_jobs(runtime_job_id) ON DELETE SET NULL,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_university_watchers_uni_id
    ON university_watchers(university_id);

CREATE INDEX IF NOT EXISTS idx_university_watchers_enabled_next
    ON university_watchers(enabled, next_check_at)
    WHERE enabled = TRUE;

COMMENT ON TABLE university_watchers IS
    'Phase 13 — Autonomous Monitoring Engine: per-university lightweight probe state';
"""


async def _apply() -> None:
    engine = create_migration_engine(pool_size=1, max_overflow=0, future=True)
    try:
        async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        async with async_session() as db:
            # This constant contains only ordinary DDL, not procedural bodies.
            for statement in SQL.split(";"):
                if statement.strip():
                    await db.execute(text(statement.strip()))
            await db.commit()
        print("Migration 032 applied — university_watchers table ready.")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(_apply())
