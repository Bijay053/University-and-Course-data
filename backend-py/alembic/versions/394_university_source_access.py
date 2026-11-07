"""Shared, credential-free university document access registry."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "394_university_source_access"
down_revision = "393_scrape_runtime_controls"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "university_source_access",
        sa.Column("origin", sa.Text(), primary_key=True),
        sa.Column("url", sa.Text(), primary_key=True),
        sa.Column("policy", postgresql.JSONB(), nullable=False),
        sa.Column("observation", postgresql.JSONB(), nullable=False),
        sa.Column("history", postgresql.JSONB(), nullable=False),
        sa.Column("last_success_at", sa.DateTime(timezone=True)),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )


def downgrade():
    op.drop_table("university_source_access")
