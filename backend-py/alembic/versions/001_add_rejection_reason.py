"""Add rejection_reason column to scraped_courses.

Revision ID: 001_add_rejection_reason
Revises:
Create Date: 2026-04-26
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "001_add_rejection_reason"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "scraped_courses",
        sa.Column("rejection_reason", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("scraped_courses", "rejection_reason")
