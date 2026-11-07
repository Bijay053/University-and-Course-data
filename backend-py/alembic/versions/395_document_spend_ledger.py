"""Persist the daily dollar budget required by document matching."""
from alembic import op
import sqlalchemy as sa

revision = "395_document_spend_ledger"
down_revision = "394_university_source_access"
branch_labels = None
depends_on = None


def upgrade():
    if not sa.inspect(op.get_bind()).has_table("scrape_agent_spend"):
        op.create_table(
            "scrape_agent_spend",
            sa.Column("id", sa.Text(), primary_key=True),
            sa.Column("day", sa.Date(), nullable=False),
            sa.Column("thread_id", sa.Text(), nullable=False),
            sa.Column("model", sa.Text(), nullable=False),
            sa.Column("reserved_usd", sa.Numeric(18, 8), nullable=False),
            sa.Column("charged_usd", sa.Numeric(18, 8), nullable=False),
            sa.Column("status", sa.Text(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                      server_default=sa.func.now()),
        )
    op.execute("CREATE INDEX IF NOT EXISTS ix_scrape_agent_spend_day ON scrape_agent_spend (day)")


def downgrade():
    raise RuntimeError("The financial audit ledger must be retained; a reviewed retention plan is required.")
