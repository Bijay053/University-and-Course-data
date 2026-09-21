"""Retain authoritative revoked generations for Redis lock replacement."""
from alembic import op

revision = "383_claim_lock_lineage"
down_revision = "382_worker_claims"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        ALTER TABLE autonomous_worker_claims
        ADD COLUMN IF NOT EXISTS revoked_generations JSONB NOT NULL DEFAULT '[]'::jsonb
    """)


def downgrade():
    op.drop_column("autonomous_worker_claims", "revoked_generations")