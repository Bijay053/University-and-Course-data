#!/usr/bin/env python3
"""Migration 029 — Phase 10: Change Detection Engine tables.

Creates:
  - course_snapshots       — immutable per-job snapshot of key course fields
  - course_change_events   — detected changes between consecutive snapshots

Apply on production:
    cd /root/University-and-Course-data
    PYTHONPATH=backend-py python3 backend-py/scripts/apply_migration_029.py
"""
import asyncio
import os

_raw_url = os.environ.get(
    "DATABASE_URL",
    "postgresql+asyncpg://postgres:password@127.0.0.1:5432/university_portal",
)
DATABASE_URL = _raw_url.replace("postgresql://", "postgresql+asyncpg://", 1)

_STMTS = [
    # ── course_snapshots ─────────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS course_snapshots (
        id                          SERIAL PRIMARY KEY,
        university_id               INTEGER NOT NULL
                                        REFERENCES universities(id) ON DELETE CASCADE,
        scrape_job_id               TEXT NOT NULL,
        course_id                   INTEGER REFERENCES courses(id) ON DELETE SET NULL,
        course_name                 TEXT NOT NULL,
        course_url                  TEXT,
        international_fee           FLOAT,
        fee_term                    TEXT,
        duration                    FLOAT,
        duration_term               TEXT,
        intake_months               JSONB,
        ielts_overall               FLOAT,
        pte_overall                 FLOAT,
        toefl_overall               FLOAT,
        academic_score              FLOAT,
        academic_level              TEXT,
        other_requirement           TEXT,
        course_location             TEXT,
        study_mode                  TEXT,
        degree_level                TEXT,
        avg_verification_confidence FLOAT,
        auto_publish_status         TEXT,
        page_hash                   TEXT,
        snapshotted_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_course_snapshots_uni_job ON course_snapshots(university_id, scrape_job_id)",
    "CREATE INDEX IF NOT EXISTS ix_course_snapshots_job    ON course_snapshots(scrape_job_id)",

    # ── course_change_events ─────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS course_change_events (
        id                  SERIAL PRIMARY KEY,
        university_id       INTEGER NOT NULL
                                REFERENCES universities(id) ON DELETE CASCADE,
        course_id           INTEGER REFERENCES courses(id) ON DELETE SET NULL,
        course_name         TEXT NOT NULL,
        scrape_job_id       TEXT NOT NULL,
        field_name          TEXT NOT NULL,
        old_value           TEXT,
        new_value           TEXT,
        change_type         TEXT NOT NULL,
        severity            TEXT NOT NULL,
        confidence_before   FLOAT,
        confidence_after    FLOAT,
        detected_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        status              TEXT NOT NULL DEFAULT 'new'
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_cce_uni_severity  ON course_change_events(university_id, severity)",
    "CREATE INDEX IF NOT EXISTS ix_cce_job            ON course_change_events(scrape_job_id)",
    "CREATE INDEX IF NOT EXISTS ix_cce_uni_detected   ON course_change_events(university_id, detected_at)",

    "COMMENT ON TABLE course_snapshots IS 'Phase 10: immutable key-field snapshot per scrape job'",
    "COMMENT ON TABLE course_change_events IS 'Phase 10: detected changes between consecutive scrape snapshots'",
]


async def main() -> None:
    from sqlalchemy import text
    from app.database import engine
    async with engine.begin() as conn:
        for stmt in _STMTS:
            await conn.execute(text(stmt.strip()))
    print("Migration 029 applied — course_snapshots + course_change_events created.")


if __name__ == "__main__":
    asyncio.run(main())
