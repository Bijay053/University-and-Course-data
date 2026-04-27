"""Add requeue_count column to scrape_runtime_jobs.

Revision ID: 003_add_requeue_count
Revises: 002_add_extraction_method
Create Date: 2026-04-27
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "003_add_requeue_count"
down_revision = "002_add_extraction_method"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "scrape_runtime_jobs",
        sa.Column("requeue_count", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("scrape_runtime_jobs", "requeue_count")
