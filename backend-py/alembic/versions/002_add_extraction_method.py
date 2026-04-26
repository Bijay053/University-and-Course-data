"""Add extraction_method column to scraped_courses.

Revision ID: 002_add_extraction_method
Revises: 001_add_rejection_reason
Create Date: 2026-04-26
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "002_add_extraction_method"
down_revision = "001_add_rejection_reason"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "scraped_courses",
        sa.Column("extraction_method", JSONB(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("scraped_courses", "extraction_method")
