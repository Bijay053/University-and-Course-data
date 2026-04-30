"""Fix float32 precision leak in courses.duration column.

courses.duration was declared as Float (SQLAlchemy) which maps to
PostgreSQL REAL (4-byte, 32-bit float).  Values like 1.7 were stored
as 1.7000000476837158 — the canonical IEEE-754 float32 round-trip
signature.

Fix: ALTER the column to NUMERIC(6, 2) (exact decimal, up to 9999.99).
Existing values are rounded to 2 decimal places during the migration so
1.7000000476837158 becomes 1.70, 0.699999988079071 becomes 0.70, etc.

The materialized view ``course_search_view`` depends on the duration column
so it must be dropped before the ALTER and recreated afterwards using the
definition captured from ``pg_matviews``.

NOTE: On production this migration was applied manually via psql (postgres
superuser) because:
  a) The uniportal role lacks GRANT on alembic_version.
  b) The ConfigParser % interpolation bug in env.py (fixed in the same
     commit) prevented venv/bin/alembic from running at all.

Revision ID: 012_duration_numeric
Revises: 011_gemini_cost_tracking
Create Date: 2026-04-30
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "012_duration_numeric"
down_revision = "011_gemini_cost_tracking"
branch_labels = None
depends_on = None

# The materialized view ``course_search_view`` depends on ``courses.duration``.
# We capture its definition before dropping so we can recreate it identically.
_GET_VIEW_DEF = """
    SELECT definition
    FROM pg_matviews
    WHERE matviewname = 'course_search_view'
"""

_GET_VIEW_INDEXES = """
    SELECT indexdef
    FROM pg_indexes
    WHERE tablename = 'course_search_view'
"""


def upgrade() -> None:
    conn = op.get_bind()

    # 1. Capture the view definition and any indexes before dropping.
    view_row = conn.execute(sa.text(_GET_VIEW_DEF)).fetchone()
    view_def = view_row[0] if view_row else None

    idx_rows = conn.execute(sa.text(_GET_VIEW_INDEXES)).fetchall()
    idx_defs = [r[0] for r in idx_rows] if idx_rows else []

    # 2. Drop the view (removes the dependency on the duration column).
    if view_def:
        op.execute("DROP MATERIALIZED VIEW IF EXISTS course_search_view")

    # 3. Alter the column type.
    op.execute("""
        ALTER TABLE courses
        ALTER COLUMN duration
        TYPE NUMERIC(6, 2)
        USING ROUND(duration::numeric, 2)
    """)

    # 4. Recreate the view using the captured definition.
    if view_def:
        op.execute(
            f"CREATE MATERIALIZED VIEW course_search_view AS {view_def} WITH DATA"
        )
        for idx_def in idx_defs:
            op.execute(idx_def)
        # Re-grant read access to the application role.
        op.execute("GRANT SELECT ON course_search_view TO uniportal")


def downgrade() -> None:
    conn = op.get_bind()

    view_row = conn.execute(sa.text(_GET_VIEW_DEF)).fetchone()
    view_def = view_row[0] if view_row else None
    idx_rows = conn.execute(sa.text(_GET_VIEW_INDEXES)).fetchall()
    idx_defs = [r[0] for r in idx_rows] if idx_rows else []

    if view_def:
        op.execute("DROP MATERIALIZED VIEW IF EXISTS course_search_view")

    op.execute("""
        ALTER TABLE courses
        ALTER COLUMN duration
        TYPE REAL
        USING duration::real
    """)

    if view_def:
        op.execute(
            f"CREATE MATERIALIZED VIEW course_search_view AS {view_def} WITH DATA"
        )
        for idx_def in idx_defs:
            op.execute(idx_def)
        op.execute("GRANT SELECT ON course_search_view TO uniportal")
