"""Preserve published tuition's intake applicability without rewriting old fees."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "396_fee_intake_applicability"
down_revision = "395_document_spend_ledger"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "fee_intake_applicability",
        sa.Column("fee_id", sa.Integer(), sa.ForeignKey("fees.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("entry_year", sa.Integer(), nullable=False),
        sa.Column("months", postgresql.JSONB(), nullable=False),
        sa.Column("source_urls", postgresql.JSONB(), nullable=False),
        sa.Column("source_digest", sa.Text(), nullable=False),
        sa.CheckConstraint("entry_year BETWEEN 2000 AND 2100", name="ck_fee_intake_year"),
        sa.CheckConstraint("jsonb_typeof(months) = 'array' AND jsonb_array_length(months) > 0",
                           name="ck_fee_intake_months"),
    )


def downgrade():
    # Never silently discard mappings already used by approved courses.
    if op.get_bind().execute(sa.text("SELECT EXISTS (SELECT 1 FROM fee_intake_applicability)")).scalar():
        raise RuntimeError("Cannot downgrade while published intake-fee mappings exist.")
    op.drop_table("fee_intake_applicability")
