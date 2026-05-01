"""Fix float32 precision in scraped_courses.duration column.

Migration 012 fixed courses.duration (REAL → NUMERIC(6,2)) but missed
scraped_courses.duration which remained as REAL (float32).  This caused
values like 1.7 to be stored as 1.7000000476837158 in the staging table
and displayed verbatim in the admin review screen.

The courses.duration column (promoted values) was already correct.
This migration aligns scraped_courses with the same NUMERIC(6,2) type.

Revision ID: 014_scraped_courses_duration_numeric
Revises: 013_discovery_failure_alerts
Create Date: 2026-05-01
"""
from __future__ import annotations

from alembic import op

revision = "014_scraped_courses_duration_numeric"
down_revision = "013_discovery_failure_alerts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE scraped_courses
        ALTER COLUMN duration
        TYPE NUMERIC(6, 2)
        USING ROUND(duration::numeric, 2)
    """)


def downgrade() -> None:
    op.execute("""
        ALTER TABLE scraped_courses
        ALTER COLUMN duration
        TYPE REAL
        USING duration::real
    """)
