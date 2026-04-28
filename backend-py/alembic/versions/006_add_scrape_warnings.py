"""Add scrape_warnings JSONB column to scraped_courses.

Stores a list of machine-readable warning codes produced by the scraper
when it detects a likely data quality issue (English section found in
HTML but all scores are blank, fee section found but fee is blank,
suspicious duration, etc.). The review UI surfaces these as amber badges
so operators know why a row requires manual verification before approval.

Revision ID: 006_add_scrape_warnings
Revises: 005_add_claim_count
Create Date: 2026-04-28
"""
from __future__ import annotations

from alembic import op

revision = "006_add_scrape_warnings"
down_revision = "005_add_claim_count"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE scraped_courses"
        " ADD COLUMN IF NOT EXISTS scrape_warnings JSONB"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE scraped_courses DROP COLUMN IF EXISTS scrape_warnings"
    )
