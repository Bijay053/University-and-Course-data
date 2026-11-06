"""Isolate live job controls from request evidence and progress checkpoints."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "393_scrape_runtime_controls"
down_revision = "392_insight_error_reports"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "scrape_runtime_controls",
        sa.Column("runtime_job_id", sa.Text(),
                  sa.ForeignKey("scrape_runtime_jobs.runtime_job_id", ondelete="CASCADE"),
                  primary_key=True),
        sa.Column("state", postgresql.JSONB(), nullable=False),
    )


def downgrade():
    op.drop_table("scrape_runtime_controls")
