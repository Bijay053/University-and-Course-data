"""Add claim_count column to scrape_runtime_jobs (healing migration).

Environments that ran the original 003 migration (which only added requeue_count)
will be missing claim_count. This migration ensures the column exists regardless
of migration history. Fresh environments where 003 already adds claim_count are
unaffected thanks to the IF NOT EXISTS guard.

Revision ID: 005_add_claim_count
Revises: 004_add_requeue_events
Create Date: 2026-04-27
"""
from __future__ import annotations

from alembic import op

revision = "005_add_claim_count"
down_revision = "004_add_requeue_events"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE scrape_runtime_jobs"
        " ADD COLUMN IF NOT EXISTS claim_count INTEGER NOT NULL DEFAULT 0"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE scrape_runtime_jobs DROP COLUMN IF EXISTS claim_count"
    )
