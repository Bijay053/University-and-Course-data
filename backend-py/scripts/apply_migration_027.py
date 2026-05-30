#!/usr/bin/env python3
"""Migration 027 — Phase 9 Conflict Repair Loop: conflict_repair_log table.

Creates:
  - conflict_repair_log  — one row per (course, field) repair attempt.
    UNIQUE (scraped_course_id, field_name) ensures at most one attempt per field,
    preventing endless repair loops.

Apply on production:
    cd /root/University-and-Course-data
    PYTHONPATH=backend-py python3 backend-py/scripts/apply_migration_027.py
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
    CREATE TABLE IF NOT EXISTS conflict_repair_log (
        id                  SERIAL  PRIMARY KEY,
        scraped_course_id   INTEGER NOT NULL
                            REFERENCES scraped_courses(id) ON DELETE CASCADE,
        field_name          VARCHAR(100) NOT NULL,
        attempted_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        diagnosis           VARCHAR(60),
        action_taken        VARCHAR(40),
        resolved            BOOLEAN NOT NULL DEFAULT FALSE,
        confidence_before   INTEGER,
        confidence_after    INTEGER,
        conflicting_sources JSONB,
        resolved_value      TEXT,
        CONSTRAINT crlog_course_field_uniq UNIQUE (scraped_course_id, field_name)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_crlog_sc   ON conflict_repair_log (scraped_course_id)",
    "CREATE INDEX IF NOT EXISTS idx_crlog_res  ON conflict_repair_log (resolved)",
    """
    COMMENT ON TABLE conflict_repair_log IS
        'Phase 9 Conflict Repair Loop — one row per (course, field) repair attempt. '
        'UNIQUE constraint prevents re-running repair for the same field.'
    """,
]


async def run() -> None:
    from app.database import engine
    from sqlalchemy import text

    async with engine.begin() as conn:
        for stmt in _STMTS:
            await conn.execute(text(stmt))
    print("Migration 027 applied: conflict_repair_log table created.")


if __name__ == "__main__":
    asyncio.run(run())
