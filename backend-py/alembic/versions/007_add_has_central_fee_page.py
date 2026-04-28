"""Add has_central_fee_page boolean to scraped_courses.

Persists the flag set by bond_static_extract / ECU / CSU pipelines when a
university publishes all course fees on a single central page rather than on
each individual course page.  Without this column the flag was only present
in the in-memory staging payload and was lost after commit, which meant the
confidence gate in the approve endpoint could not distinguish Bond/ECU/CSU
courses (which intentionally have a NULL international_fee after staging)
from genuinely incomplete rows.

Revision ID: 007_add_has_central_fee_page
Revises: 006_add_scrape_warnings
Create Date: 2026-04-28
"""
from __future__ import annotations

from alembic import op

revision = "007_add_has_central_fee_page"
down_revision = "006_add_scrape_warnings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE scraped_courses"
        " ADD COLUMN IF NOT EXISTS has_central_fee_page BOOLEAN NOT NULL DEFAULT FALSE"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE scraped_courses DROP COLUMN IF EXISTS has_central_fee_page"
    )
