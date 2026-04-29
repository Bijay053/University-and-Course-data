"""Add cricos_code column to scraped_courses.

Priority 3 — CRICOS code matching for AU PDF course fee tables.

Changes:
  scraped_courses.cricos_code  TEXT NULL  — CRICOS code extracted from the
                                            course page (e.g. "084932E").
                                            Used for CRICOS-first lookup when
                                            matching against fee_by_course dicts
                                            (which _pick_per_course_amounts
                                            already keys by CRICOS code).
  Index: ix_scraped_courses_cricos_code   — speeds up lookups by CRICOS code.

Revision ID: 010_add_cricos_code
Revises: 009_add_metrics_alerts
Create Date: 2026-04-29
"""
from __future__ import annotations

from alembic import op

revision = "010_add_cricos_code"
down_revision = "009_add_metrics_alerts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE scraped_courses
        ADD COLUMN IF NOT EXISTS cricos_code TEXT NULL;
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS ix_scraped_courses_cricos_code
        ON scraped_courses (cricos_code)
        WHERE cricos_code IS NOT NULL;
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_scraped_courses_cricos_code;")
    op.execute("ALTER TABLE scraped_courses DROP COLUMN IF EXISTS cricos_code;")
