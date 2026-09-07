"""Prevent duplicate review rows within one scrape job.

Revision ID: 380_job_url_unique
Revises: 378_url_identity
"""
from alembic import op
import sqlalchemy as sa


revision = "380_job_url_unique"
down_revision = "378_url_identity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Keep the oldest unreviewed row if historical races already produced
    # duplicates. Reviewed rows are deliberately outside this constraint.
    op.execute(
        sa.text(
            """
            DELETE FROM scraped_courses AS duplicate
            USING scraped_courses AS keeper
            WHERE duplicate.id > keeper.id
              AND duplicate.university_id = keeper.university_id
              AND duplicate.scrape_job_id = keeper.scrape_job_id
              AND duplicate.canonical_course_url = keeper.canonical_course_url
              AND duplicate.canonical_course_url IS NOT NULL
              AND duplicate.status NOT IN ('approved', 'published')
              AND keeper.status NOT IN ('approved', 'published')
            """
        )
    )
    op.drop_index(
        "ix_scraped_courses_review_url_identity",
        table_name="scraped_courses",
    )
    op.create_index(
        "uq_scraped_courses_job_review_url_identity",
        "scraped_courses",
        ["university_id", "canonical_course_url", "scrape_job_id"],
        unique=True,
        postgresql_where=sa.text("status NOT IN ('approved', 'published')"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_scraped_courses_job_review_url_identity",
        table_name="scraped_courses",
    )
    op.create_index(
        "ix_scraped_courses_review_url_identity",
        "scraped_courses",
        ["university_id", "canonical_course_url"],
        unique=False,
        postgresql_where=sa.text("status NOT IN ('approved', 'published')"),
    )