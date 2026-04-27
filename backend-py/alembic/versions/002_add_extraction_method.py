"""Add extraction_method column to scraped_courses.

Revision ID: 002_add_extraction_method
Revises: 001_add_rejection_reason
Create Date: 2026-04-26
"""
from __future__ import annotations

from alembic import op

revision = "002_add_extraction_method"
down_revision = "001_add_rejection_reason"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE scraped_courses ADD COLUMN IF NOT EXISTS extraction_method JSONB"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE scraped_courses DROP COLUMN IF EXISTS extraction_method"
    )
