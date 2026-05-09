"""Add columns to courses table that were added to the model without a migration.

The courses table was bootstrapped from an older SQLAlchemy model snapshot.
Several columns present in the ORM model never had a corresponding Alembic
migration, so they exist on the dev DB (created via create_all at setup time)
but are absent on the production DB, causing every GET /api/courses request to
fail with a 500 (UndefinedColumn ProgrammingError).

This migration is fully idempotent: every statement uses ADD COLUMN IF NOT
EXISTS so it is safe to run multiple times.

Affected columns (all on the ``courses`` table):
  - sub_category
  - course_structure
  - career_outcomes
  - course_location
  - student_market
  - delivery_mode
  - international_eligible
  - on_campus_available
  - eligibility_status     (NOT NULL DEFAULT 'unknown')
  - eligibility_reason
  - eligibility_confidence
  - approval_status        (NOT NULL DEFAULT 'approved')
  - approval_score
  - approved_at
  - last_reviewed_at
  - last_edited_at
  - last_edited_by

Revision ID: 015_courses_missing_columns
Revises: 014_scraped_courses_duration_numeric
Create Date: 2026-05-04
"""
from __future__ import annotations

from alembic import op

revision = "015_courses_missing_columns"
down_revision = "014_scraped_courses_duration_numeric"
branch_labels = None
depends_on = None

_UPGRADE_SQL = """
ALTER TABLE courses ADD COLUMN IF NOT EXISTS sub_category         TEXT;
ALTER TABLE courses ADD COLUMN IF NOT EXISTS course_structure     TEXT;
ALTER TABLE courses ADD COLUMN IF NOT EXISTS career_outcomes      TEXT;
ALTER TABLE courses ADD COLUMN IF NOT EXISTS course_location      TEXT;
ALTER TABLE courses ADD COLUMN IF NOT EXISTS student_market       TEXT;
ALTER TABLE courses ADD COLUMN IF NOT EXISTS delivery_mode        TEXT;
ALTER TABLE courses ADD COLUMN IF NOT EXISTS international_eligible BOOLEAN;
ALTER TABLE courses ADD COLUMN IF NOT EXISTS on_campus_available  BOOLEAN;
ALTER TABLE courses ADD COLUMN IF NOT EXISTS eligibility_reason   TEXT;
ALTER TABLE courses ADD COLUMN IF NOT EXISTS eligibility_confidence FLOAT;
ALTER TABLE courses ADD COLUMN IF NOT EXISTS approval_score       FLOAT;
ALTER TABLE courses ADD COLUMN IF NOT EXISTS approved_at          TIMESTAMPTZ;
ALTER TABLE courses ADD COLUMN IF NOT EXISTS last_reviewed_at     TIMESTAMPTZ;
ALTER TABLE courses ADD COLUMN IF NOT EXISTS last_edited_at       TIMESTAMPTZ;
ALTER TABLE courses ADD COLUMN IF NOT EXISTS last_edited_by       TEXT;

-- NOT NULL columns require a default so existing rows satisfy the constraint.
ALTER TABLE courses ADD COLUMN IF NOT EXISTS eligibility_status
    TEXT NOT NULL DEFAULT 'unknown';
ALTER TABLE courses ADD COLUMN IF NOT EXISTS approval_status
    TEXT NOT NULL DEFAULT 'approved';
"""

_DOWNGRADE_SQL = """
ALTER TABLE courses DROP COLUMN IF EXISTS sub_category;
ALTER TABLE courses DROP COLUMN IF EXISTS course_structure;
ALTER TABLE courses DROP COLUMN IF EXISTS career_outcomes;
ALTER TABLE courses DROP COLUMN IF EXISTS course_location;
ALTER TABLE courses DROP COLUMN IF EXISTS student_market;
ALTER TABLE courses DROP COLUMN IF EXISTS delivery_mode;
ALTER TABLE courses DROP COLUMN IF EXISTS international_eligible;
ALTER TABLE courses DROP COLUMN IF EXISTS on_campus_available;
ALTER TABLE courses DROP COLUMN IF EXISTS eligibility_reason;
ALTER TABLE courses DROP COLUMN IF EXISTS eligibility_confidence;
ALTER TABLE courses DROP COLUMN IF EXISTS approval_score;
ALTER TABLE courses DROP COLUMN IF EXISTS approved_at;
ALTER TABLE courses DROP COLUMN IF EXISTS last_reviewed_at;
ALTER TABLE courses DROP COLUMN IF EXISTS last_edited_at;
ALTER TABLE courses DROP COLUMN IF EXISTS last_edited_by;
ALTER TABLE courses DROP COLUMN IF EXISTS eligibility_status;
ALTER TABLE courses DROP COLUMN IF EXISTS approval_status;
"""


def upgrade() -> None:
    op.execute(_UPGRADE_SQL)


def downgrade() -> None:
    op.execute(_DOWNGRADE_SQL)
