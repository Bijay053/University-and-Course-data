"""Private review queue for errors reported by toolkit visitors."""
from alembic import op
import sqlalchemy as sa

revision = "391_public_course_reports"
down_revision = "390_qualification_guidance"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "course_error_reports",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("course_id", sa.Integer(), sa.ForeignKey("courses.id", ondelete="SET NULL")),
        sa.Column("course_name", sa.Text(), nullable=False),
        sa.Column("university_name", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("field", sa.Text(), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("source_url", sa.Text()),
        sa.Column("status", sa.Text(), nullable=False, server_default="new"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("status IN ('new', 'reviewed', 'resolved')", name="ck_course_error_report_status"),
        sa.CheckConstraint("kind IN ('missing', 'incorrect')", name="ck_course_error_report_kind"),
    )
    op.create_index("ix_course_error_reports_status_created", "course_error_reports", ["status", "created_at"])


def downgrade():
    op.drop_index("ix_course_error_reports_status_created", table_name="course_error_reports")
    op.drop_table("course_error_reports")
