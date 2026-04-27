"""Add requeue_events JSONB column to scrape_runtime_jobs.

Revision ID: 004_add_requeue_events
Revises: 003_add_requeue_count
Create Date: 2026-04-27
"""
from __future__ import annotations

from alembic import op

revision = "004_add_requeue_events"
down_revision = "003_add_requeue_count"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE scrape_runtime_jobs ADD COLUMN IF NOT EXISTS requeue_events JSONB"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE scrape_runtime_jobs DROP COLUMN IF EXISTS requeue_events"
    )
