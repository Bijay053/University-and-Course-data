#!/usr/bin/env python3
"""Migration 025 — Phase 9 Verification & Confidence Engine schema.

Creates:
  - field_verification_results  — per-field cross-source verification outcomes
Adds column:
  - scraped_courses.avg_verification_confidence  — pre-computed average for fast querying

Apply on production:
    cd /root/University-and-Course-data
    PYTHONPATH=backend-py python3 backend-py/scripts/apply_migration_025.py
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
    CREATE TABLE IF NOT EXISTS field_verification_results (
        id                  SERIAL  PRIMARY KEY,
        scraped_course_id   INTEGER NOT NULL
                            REFERENCES scraped_courses(id) ON DELETE CASCADE,
        field_name          VARCHAR(100) NOT NULL,
        verified_value      TEXT,
        confidence          INTEGER NOT NULL DEFAULT 0,
        status              VARCHAR(20) NOT NULL DEFAULT 'needs_review',
        source_count        INTEGER NOT NULL DEFAULT 0,
        sources             JSONB   NOT NULL DEFAULT '[]',
        conflict_sources    JSONB,
        verification_time   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        CONSTRAINT fvr_course_field_uniq UNIQUE (scraped_course_id, field_name)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_fvr_course ON field_verification_results (scraped_course_id)",
    "CREATE INDEX IF NOT EXISTS idx_fvr_status  ON field_verification_results (status)",
    """
    COMMENT ON TABLE field_verification_results IS
        'Phase 9 — per-field cross-source verification: confidence score, status '
        '(verified/likely_correct/needs_review/conflict), and contributing sources.'
    """,
    # Add avg_verification_confidence to scraped_courses for fast auto-publish gate
    """
    ALTER TABLE scraped_courses
        ADD COLUMN IF NOT EXISTS avg_verification_confidence FLOAT
    """,
    """
    COMMENT ON COLUMN scraped_courses.avg_verification_confidence IS
        'Phase 9 — average cross-source confidence across all verified fields (0-100). '
        'Used by auto-publish gate: must be >= 85 to auto-publish.'
    """,
]


async def run() -> None:
    from app.database import engine
    from sqlalchemy import text

    async with engine.begin() as conn:
        for stmt in _STMTS:
            await conn.execute(text(stmt))
    print("Migration 025 applied: field_verification_results + avg_verification_confidence.")


if __name__ == "__main__":
    asyncio.run(run())
