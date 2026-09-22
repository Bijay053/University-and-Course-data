"""Add durable evidence-backed staged requirement status."""
from alembic import op

revision = "384_requirement_status"
down_revision = "383_claim_lock_lineage"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        ALTER TABLE scraped_courses
        ADD COLUMN IF NOT EXISTS requirement_status JSONB
    """)


def downgrade():
    op.drop_column("scraped_courses", "requirement_status")