"""Add discovery_failure_alerts table (Tier 7 operator alert).

Persists a diagnostic payload whenever all discovery tiers (BFS, sitemap,
alt-paths, subdomain probes) complete with fewer than 3 course candidates.
Surfaces silent zero/near-zero discovery failures loudly in the admin UI
instead of burying them in logs.

NOTE: On production apply manually (uniportal role lacks GRANT on
alembic_version):

    sudo -u postgres psql university_portal << 'SQL'
    CREATE TABLE IF NOT EXISTS discovery_failure_alerts (
        id               SERIAL PRIMARY KEY,
        university_id    INTEGER NOT NULL REFERENCES universities(id) ON DELETE CASCADE,
        candidates_found INTEGER NOT NULL,
        diagnostic       JSONB   NOT NULL,
        created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
        resolved_at      TIMESTAMPTZ,
        resolved_by      TEXT
    );
    CREATE INDEX IF NOT EXISTS ix_discovery_failure_alerts_university_id
        ON discovery_failure_alerts (university_id);
    SQL

Revision ID: 013_discovery_failure_alerts
Revises: 012_duration_numeric
Create Date: 2026-05-01
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "013_discovery_failure_alerts"
down_revision = "012_duration_numeric"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "discovery_failure_alerts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "university_id",
            sa.Integer(),
            sa.ForeignKey("universities.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("candidates_found", sa.Integer(), nullable=False),
        sa.Column("diagnostic", JSONB(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_by", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("discovery_failure_alerts")
