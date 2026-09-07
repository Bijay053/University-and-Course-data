"""Index canonical scraped-course URL identity.

Revision ID: 378_url_identity
Revises: 359_snapshot_health
"""
from alembic import op
import sqlalchemy as sa

from app.services.scraper.url_identity import canonical_course_url_key


revision = "378_url_identity"
down_revision = "359_snapshot_health"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "scraped_courses",
        sa.Column("canonical_course_url", sa.Text(), nullable=True),
    )

    connection = op.get_bind()
    select_batch = sa.text(
        "SELECT id, course_website FROM scraped_courses "
        "WHERE course_website IS NOT NULL AND id > :last_id "
        "ORDER BY id LIMIT 1000"
    )
    update_statement = sa.text(
        "UPDATE scraped_courses SET canonical_course_url = :url_key "
        "WHERE id = :row_id"
    )
    last_id = 0
    while batch := connection.execute(
        select_batch,
        {"last_id": last_id},
    ).fetchall():
        updates = [
            {
                "row_id": row.id,
                "url_key": canonical_course_url_key(row.course_website) or None,
            }
            for row in batch
        ]
        connection.execute(
            update_statement,
            updates,
        )
        last_id = batch[-1].id

    op.create_index(
        "ix_scraped_courses_review_url_identity",
        "scraped_courses",
        ["university_id", "canonical_course_url"],
        unique=False,
        postgresql_where=sa.text("status NOT IN ('approved', 'published')"),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_scraped_courses_review_url_identity",
        table_name="scraped_courses",
    )
    op.drop_column("scraped_courses", "canonical_course_url")