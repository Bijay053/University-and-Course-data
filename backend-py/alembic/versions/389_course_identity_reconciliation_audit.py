"""Durable before/after backups for reviewed course-ID reconciliation."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "389_course_identity_reconciliation_audit"
down_revision = "388_course_id_aliases"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "course_identity_reconciliation_audit",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("audit_id", postgresql.UUID(as_uuid=False), nullable=False, index=True),
        sa.Column("manifest_sha256", sa.Text(), nullable=False),
        sa.Column("approval_revision", sa.Text(), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("entity_table", sa.Text(), nullable=False),
        sa.Column("entity_key", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("row_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "event_type IN ('before', 'after', 'run')",
            name="ck_course_identity_reconciliation_audit_event",
        ),
        sa.CheckConstraint(
            "manifest_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_course_identity_reconciliation_audit_manifest_sha",
        ),
    )
    op.create_index(
        "ix_course_identity_reconciliation_audit_manifest",
        "course_identity_reconciliation_audit",
        ["manifest_sha256", "created_at"],
    )


def downgrade():
    op.execute(
        """
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM course_identity_reconciliation_audit) THEN
                RAISE EXCEPTION
                    'Export and preserve reconciliation audit backups before downgrading';
            END IF;
        END $$;
        """
    )
    op.drop_index(
        "ix_course_identity_reconciliation_audit_manifest",
        table_name="course_identity_reconciliation_audit",
    )
    op.drop_table("course_identity_reconciliation_audit")