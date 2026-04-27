"""Add requeue_events JSONB column to scrape_runtime_jobs.

Revision ID: 004_add_requeue_events
Revises: 003_add_requeue_count
Create Date: 2026-04-27
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "004_add_requeue_events"
down_revision = "003_add_requeue_count"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "scrape_runtime_jobs",
        sa.Column("requeue_events", JSONB(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("scrape_runtime_jobs", "requeue_events")
