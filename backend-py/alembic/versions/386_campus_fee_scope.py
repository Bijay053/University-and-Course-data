"""Preserve independent campus fee groups at the staged URL identity boundary."""
from alembic import op
import sqlalchemy as sa

revision = "386_campus_fee_scope"
down_revision = "385_dated_review_history"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("scraped_courses", sa.Column("fee_scope_key", sa.Text(), nullable=False, server_default=""))
    op.drop_index("uq_scraped_courses_job_review_url_identity", table_name="scraped_courses")
    op.create_index(
        "uq_scraped_courses_job_review_url_identity", "scraped_courses",
        ["university_id", "canonical_course_url", "scrape_job_id", "fee_scope_key"],
        unique=True, postgresql_where=sa.text("status NOT IN ('approved', 'published')"),
    )


def downgrade():
    # Do not silently delete siblings to fit the old identity.
    op.execute("""
        DO $$ BEGIN
          IF EXISTS (SELECT 1 FROM scraped_courses WHERE fee_scope_key <> '') THEN
            RAISE EXCEPTION 'Resolve campus fee groups before downgrading';
          END IF;
        END $$;
    """)
    op.drop_index("uq_scraped_courses_job_review_url_identity", table_name="scraped_courses")
    op.create_index(
        "uq_scraped_courses_job_review_url_identity", "scraped_courses",
        ["university_id", "canonical_course_url", "scrape_job_id"], unique=True,
        postgresql_where=sa.text("status NOT IN ('approved', 'published')"),
    )
    op.drop_column("scraped_courses", "fee_scope_key")