#!/usr/bin/env python3
"""Migration 030 — Phase 11: Knowledge Graph layer.

Creates:
  - course_pathways          — articulation / prerequisite links between courses
  - course_accreditations    — professional / regulatory accreditations per course
  - courses.campus_id        — nullable FK to university_locations.id

Also runs a safe text-match campus backfill:
  Sets courses.campus_id where course_location matches university_locations.city
  or display_name (case-insensitive). Never overwrites an already-set campus_id.

Apply on production:
    cd /root/University-and-Course-data
    PYTHONPATH=backend-py python3 backend-py/scripts/apply_migration_030.py
"""
import asyncio
import os

_raw_url = os.environ.get(
    "DATABASE_URL",
    "postgresql+asyncpg://postgres:password@127.0.0.1:5432/university_portal",
)
DATABASE_URL = _raw_url.replace("postgresql://", "postgresql+asyncpg://", 1)

_STMTS = [
    # ── courses.campus_id ────────────────────────────────────────────────────
    """
    ALTER TABLE courses
        ADD COLUMN IF NOT EXISTS campus_id INTEGER
            REFERENCES university_locations(id) ON DELETE SET NULL
    """,
    "CREATE INDEX IF NOT EXISTS ix_courses_campus_id ON courses(campus_id)",

    # ── course_pathways ──────────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS course_pathways (
        id                  SERIAL PRIMARY KEY,
        source_course_id    INTEGER NOT NULL
                                REFERENCES courses(id) ON DELETE CASCADE,
        target_course_id    INTEGER NOT NULL
                                REFERENCES courses(id) ON DELETE CASCADE,
        pathway_type        TEXT NOT NULL DEFAULT 'articulation',
        notes               TEXT,
        created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        created_by          TEXT,
        CONSTRAINT uq_course_pathway UNIQUE (source_course_id, target_course_id, pathway_type)
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_course_pathways_source ON course_pathways(source_course_id)",
    "CREATE INDEX IF NOT EXISTS ix_course_pathways_target ON course_pathways(target_course_id)",

    # ── course_accreditations ────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS course_accreditations (
        id                  SERIAL PRIMARY KEY,
        course_id           INTEGER NOT NULL
                                REFERENCES courses(id) ON DELETE CASCADE,
        accrediting_body    TEXT NOT NULL,
        accreditation_type  TEXT,
        accreditation_url   TEXT,
        valid_from          DATE,
        valid_until         DATE,
        notes               TEXT,
        created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        created_by          TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_course_accreditations_course ON course_accreditations(course_id)",

    "COMMENT ON TABLE course_pathways IS 'Phase 11: articulation / prerequisite links between courses'",
    "COMMENT ON TABLE course_accreditations IS 'Phase 11: professional accreditations per course'",
    "COMMENT ON COLUMN courses.campus_id IS 'Phase 11: FK to university_locations; set by text-match backfill, never auto-overwritten'",

    # ── campus backfill (safe: only fills NULL campus_id) ───────────────────
    # Match course_location text to university_locations.city or display_name.
    # Uses ILIKE for case-insensitive substring matching.
    # Only sets campus_id where it is currently NULL — never overwrites.
    """
    UPDATE courses c
    SET campus_id = (
        SELECT ul.id
        FROM university_locations ul
        WHERE ul.university_id = c.university_id
          AND (
              c.course_location ILIKE '%' || ul.city || '%'
              OR c.course_location ILIKE '%' || ul.display_name || '%'
              OR ul.city ILIKE '%' || c.course_location || '%'
          )
        ORDER BY
            length(ul.city) DESC
        LIMIT 1
    )
    WHERE c.campus_id IS NULL
      AND c.course_location IS NOT NULL
      AND c.course_location != ''
    """,
]


async def main() -> None:
    from sqlalchemy import text
    from app.database import engine

    async with engine.begin() as conn:
        for stmt in _STMTS:
            stripped = stmt.strip()
            if stripped:
                await conn.execute(text(stripped))

    # Report backfill results
    from app.database import AsyncSessionLocal
    from sqlalchemy import text as t
    async with AsyncSessionLocal() as s:
        r = await s.execute(t(
            "SELECT COUNT(*) FROM courses WHERE campus_id IS NOT NULL"
        ))
        linked = r.scalar_one()
        r2 = await s.execute(t("SELECT COUNT(*) FROM courses"))
        total = r2.scalar_one()
    print(
        f"Migration 030 applied.\n"
        f"  Tables created: course_pathways, course_accreditations\n"
        f"  Column added:   courses.campus_id\n"
        f"  Campus backfill: {linked}/{total} courses linked to a campus"
    )


if __name__ == "__main__":
    asyncio.run(main())
