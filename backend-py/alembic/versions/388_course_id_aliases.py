"""Add safe, audited aliases for legacy published course IDs."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "388_course_id_aliases"
down_revision = "387_course_offerings"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "course_id_aliases",
        sa.Column(
            "alias_course_id",
            sa.Integer(),
            sa.ForeignKey("courses.id", ondelete="RESTRICT"),
            primary_key=True,
        ),
        sa.Column(
            "canonical_course_id",
            sa.Integer(),
            sa.ForeignKey("courses.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.Column(
            "audit_metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "alias_course_id <> canonical_course_id",
            name="ck_course_id_aliases_not_self",
        ),
    )
    op.create_index(
        "ix_course_id_aliases_canonical_course_id",
        "course_id_aliases",
        ["canonical_course_id"],
    )
    # Enforce one-hop mappings at the database boundary too. A course that is
    # already an alias cannot be a target, and a canonical with existing aliases
    # cannot itself be demoted into an alias.
    op.execute(
        """
        CREATE FUNCTION reject_course_id_alias_chain() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            PERFORM pg_advisory_xact_lock(388, 1);
            IF NEW.alias_course_id = NEW.canonical_course_id THEN
                RAISE EXCEPTION 'A course cannot be an alias of itself';
            END IF;
            IF EXISTS (
                SELECT 1 FROM course_id_aliases existing
                WHERE existing.alias_course_id <> NEW.alias_course_id
                  AND (
                    existing.alias_course_id = NEW.canonical_course_id
                    OR existing.canonical_course_id = NEW.alias_course_id
                  )
            ) THEN
                RAISE EXCEPTION 'Course ID aliases must point directly to a canonical course';
            END IF;
            RETURN NEW;
        END;
        $$;
        """
    )
    op.execute(
        """CREATE TRIGGER trg_course_id_alias_no_chain
        BEFORE INSERT OR UPDATE ON course_id_aliases
        FOR EACH ROW EXECUTE FUNCTION reject_course_id_alias_chain()"""
    )


def downgrade():
    op.execute(
        """
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM course_id_aliases) THEN
                RAISE EXCEPTION
                    'Export and reconcile course ID aliases before downgrading';
            END IF;
        END $$;
        """
    )
    op.execute("DROP TRIGGER IF EXISTS trg_course_id_alias_no_chain ON course_id_aliases")
    op.execute("DROP FUNCTION IF EXISTS reject_course_id_alias_chain()")
    op.drop_index("ix_course_id_aliases_canonical_course_id", table_name="course_id_aliases")
    op.drop_table("course_id_aliases")