"""Record discovery failure alert delivery outcomes.

Revision ID: 341_alert_delivery
Revises: a505b2e5b1f1
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "341_alert_delivery"
down_revision = "a505b2e5b1f1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("discovery_failure_alerts", sa.Column(
        "delivery_status", sa.Text(), server_default="pending", nullable=False
    ))
    op.add_column("discovery_failure_alerts", sa.Column(
        "delivery_attempts", sa.Integer(), server_default="0", nullable=False
    ))
    op.add_column("discovery_failure_alerts", sa.Column(
        "delivery_detail", postgresql.JSONB(astext_type=sa.Text()), nullable=True
    ))
    op.add_column("discovery_failure_alerts", sa.Column(
        "delivery_attempted_at", sa.DateTime(timezone=True), nullable=True
    ))


def downgrade() -> None:
    op.drop_column("discovery_failure_alerts", "delivery_attempted_at")
    op.drop_column("discovery_failure_alerts", "delivery_detail")
    op.drop_column("discovery_failure_alerts", "delivery_attempts")
    op.drop_column("discovery_failure_alerts", "delivery_status")