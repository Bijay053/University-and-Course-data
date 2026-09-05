"""Persist snapshot storage canary health.

Revision ID: 359_snapshot_health
Revises: 341_alert_delivery
"""
from alembic import op
import sqlalchemy as sa

revision = "359_snapshot_health"
down_revision = "341_alert_delivery"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "snapshot_storage_health",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("status", sa.Text(), nullable=False, server_default="unknown"),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_successful_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("consecutive_failures", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("alert_active", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("alert_last_sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("id = 1", name="ck_snapshot_storage_health_singleton"),
    )


def downgrade() -> None:
    op.drop_table("snapshot_storage_health")