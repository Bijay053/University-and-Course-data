"""Add rejection_reason column to scraped_courses.

Revision ID: 001_add_rejection_reason
Revises:
Create Date: 2026-04-26
"""
from __future__ import annotations

from alembic import op

revision = "001_add_rejection_reason"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE scraped_courses ADD COLUMN IF NOT EXISTS rejection_reason TEXT"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE scraped_courses DROP COLUMN IF EXISTS rejection_reason"
    )
