"""Sanitized evidence-bound last qualification approval attempt."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "390_qualification_guidance"
down_revision = "389_course_identity_audit"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("scraped_courses", sa.Column("last_qualification_approval", JSONB(), nullable=True))
    install_invalidation_trigger()


def install_invalidation_trigger():
    op.execute("""
        CREATE OR REPLACE FUNCTION clear_changed_qualification_guidance()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF (to_jsonb(NEW) - 'last_qualification_approval')
               IS DISTINCT FROM (to_jsonb(OLD) - 'last_qualification_approval') THEN
                NEW.last_qualification_approval := NULL;
            END IF;
            RETURN NEW;
        END;
        $$
    """)
    op.execute("DROP TRIGGER IF EXISTS clear_changed_qualification_guidance ON scraped_courses")
    op.execute("""
        CREATE TRIGGER clear_changed_qualification_guidance
        BEFORE UPDATE ON scraped_courses FOR EACH ROW
        EXECUTE FUNCTION clear_changed_qualification_guidance()
    """)


def downgrade():
    op.execute("DROP TRIGGER IF EXISTS clear_changed_qualification_guidance ON scraped_courses")
    op.execute("DROP FUNCTION IF EXISTS clear_changed_qualification_guidance()")
    op.drop_column("scraped_courses", "last_qualification_approval")