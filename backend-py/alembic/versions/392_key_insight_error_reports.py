"""Preserve the university/country context for public Key Insights reports."""
from alembic import op
import sqlalchemy as sa

revision = "392_insight_error_reports"
down_revision = "391_public_course_reports"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("course_error_reports", sa.Column("university_id", sa.Integer(), sa.ForeignKey("universities.id", ondelete="SET NULL")))
    op.add_column("course_error_reports", sa.Column("insight_id", sa.Integer(), sa.ForeignKey("assessment_notes.id", ondelete="SET NULL")))
    op.add_column("course_error_reports", sa.Column("insight_country", sa.Text()))
    op.alter_column("course_error_reports", "course_name", existing_type=sa.Text(), nullable=True)
    op.execute("""
        UPDATE course_error_reports r SET university_id=c.university_id
        FROM courses c WHERE r.course_id=c.id
    """)


def downgrade():
    op.execute("""
        UPDATE course_error_reports
        SET course_name='Key Insights: ' || COALESCE(insight_country, 'General guidance')
        WHERE course_name IS NULL
    """)
    op.alter_column("course_error_reports", "course_name", existing_type=sa.Text(), nullable=False)
    op.drop_column("course_error_reports", "insight_country")
    op.drop_column("course_error_reports", "insight_id")
    op.drop_column("course_error_reports", "university_id")
