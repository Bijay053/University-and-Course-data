"""Fix float32 precision leak in courses.duration column.

courses.duration was declared as Float (SQLAlchemy) which maps to
PostgreSQL REAL (4-byte, 32-bit float).  Values like 1.7 were stored
as 1.7000000476837158 — the canonical IEEE-754 float32 round-trip
signature.

Fix: ALTER the column to NUMERIC(6, 2) (exact decimal, up to 9999.99).
Existing values are rounded to 2 decimal places during the migration so
1.7000000476837158 becomes 1.70, 0.699999988079071 becomes 0.70, etc.

Matches the Numeric(5,4) pattern already used by fill_rate in this project.

Revision ID: 012_duration_numeric
Revises: 011_gemini_cost_tracking
Create Date: 2026-04-30
"""
from __future__ import annotations

from alembic import op

revision = "012_duration_numeric"
down_revision = "011_gemini_cost_tracking"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE courses
        ALTER COLUMN duration
        TYPE NUMERIC(6, 2)
        USING ROUND(duration::numeric, 2);
    """)


def downgrade() -> None:
    op.execute("""
        ALTER TABLE courses
        ALTER COLUMN duration
        TYPE REAL
        USING duration::real;
    """)
