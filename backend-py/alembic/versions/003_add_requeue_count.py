"""Add requeue_count and claim_count columns to scrape_runtime_jobs.

Revision ID: 003_add_requeue_count
Revises: 002_add_extraction_method
Create Date: 2026-04-27
"""
from __future__ import annotations

from alembic import op

revision = "003_add_requeue_count"
down_revision = "002_add_extraction_method"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE scrape_runtime_jobs"
        " ADD COLUMN IF NOT EXISTS requeue_count INTEGER NOT NULL DEFAULT 0"
    )
    op.execute(
        "ALTER TABLE scrape_runtime_jobs"
        " ADD COLUMN IF NOT EXISTS claim_count INTEGER NOT NULL DEFAULT 0"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE scrape_runtime_jobs DROP COLUMN IF EXISTS requeue_count"
    )
    op.execute(
        "ALTER TABLE scrape_runtime_jobs DROP COLUMN IF EXISTS claim_count"
    )
