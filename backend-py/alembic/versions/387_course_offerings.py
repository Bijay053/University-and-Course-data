"""Add campus offerings without modifying or merging any existing courses."""
from alembic import op
import sqlalchemy as sa

revision = "387_course_offerings"
down_revision = "386_campus_fee_scope"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("courses", sa.Column("offering_identity", sa.Text(), nullable=True))
    op.create_unique_constraint("uq_courses_offering_identity", "courses", ["offering_identity"])
    op.create_table(
        "course_offerings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("course_id", sa.Integer(), sa.ForeignKey("courses.id", ondelete="CASCADE"), nullable=False),
        sa.Column("location_key", sa.Text(), nullable=False),
        sa.Column("location", sa.Text(), nullable=False),
        sa.Column("fee_amount", sa.Float()),
        sa.Column("fee_currency", sa.Text()),
        sa.Column("fee_term", sa.Text()),
        sa.Column("fee_year", sa.Integer()),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.UniqueConstraint("course_id", "location_key", name="uq_course_offering_location"),
    )
    op.create_index("ix_course_offerings_course_id", "course_offerings", ["course_id"])


def downgrade():
    op.execute("""DO $$ BEGIN
      IF EXISTS (SELECT 1 FROM course_offerings) THEN
        RAISE EXCEPTION 'Export and reconcile verified offerings before downgrade';
      END IF;
    END $$;""")
    op.drop_table("course_offerings")
    op.drop_constraint("uq_courses_offering_identity", "courses", type_="unique")
    op.drop_column("courses", "offering_identity")