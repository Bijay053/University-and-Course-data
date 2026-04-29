"""Add *_accepted boolean columns to scraped_courses.

NULL = unknown (no evidence either way)
FALSE = explicitly not accepted by this university
TRUE  = explicitly accepted (rare — only set when policy page confirms)

These four columns allow the pipeline to distinguish "no Duolingo score found"
(NULL) from "university doesn't accept Duolingo" (FALSE).  When FALSE, the
sibling-cache back-fill must skip that test's slots so a neighbouring course's
Duolingo value is never propagated to a university that doesn't accept the test.

Revision ID: 008_add_test_accepted_flags
Revises: 007_add_has_central_fee_page
Create Date: 2026-04-29
"""
from __future__ import annotations

from alembic import op

revision = "008_add_test_accepted_flags"
down_revision = "007_add_has_central_fee_page"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE scraped_courses "
        "ADD COLUMN IF NOT EXISTS duolingo_accepted BOOLEAN, "
        "ADD COLUMN IF NOT EXISTS cambridge_accepted BOOLEAN, "
        "ADD COLUMN IF NOT EXISTS pte_accepted BOOLEAN, "
        "ADD COLUMN IF NOT EXISTS toefl_accepted BOOLEAN"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE scraped_courses "
        "DROP COLUMN IF EXISTS duolingo_accepted, "
        "DROP COLUMN IF EXISTS cambridge_accepted, "
        "DROP COLUMN IF EXISTS pte_accepted, "
        "DROP COLUMN IF EXISTS toefl_accepted"
    )
