"""Back academic requirements with settings academic-level options.

Revision ID: 381_acad_req_option
Revises: 380_job_url_unique
"""
from alembic import op
import sqlalchemy as sa

revision = "381_acad_req_option"
down_revision = "380_job_url_unique"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "academic_level_options",
        sa.Column("mapping_key", sa.Text(), nullable=True),
    )
    op.create_index(
        "uq_academic_level_options_mapping_key",
        "academic_level_options",
        ["mapping_key"],
        unique=True,
        postgresql_where=sa.text("mapping_key IS NOT NULL"),
    )
    op.add_column(
        "academic_requirements",
        sa.Column("academic_level_option_id", sa.Integer(), nullable=True),
    )
    op.create_index(
        "ix_academic_requirements_academic_level_option_id",
        "academic_requirements",
        ["academic_level_option_id"],
    )
    op.create_foreign_key(
        "fk_academic_requirements_academic_level_option_id",
        "academic_requirements", "academic_level_options",
        ["academic_level_option_id"], ["id"], ondelete="RESTRICT",
    )
    op.add_column(
        "scraped_courses",
        sa.Column("academic_level_option_id", sa.Integer(), nullable=True),
    )
    op.create_index(
        "ix_scraped_courses_academic_level_option_id",
        "scraped_courses",
        ["academic_level_option_id"],
    )
    op.create_foreign_key(
        "fk_scraped_courses_academic_level_option_id",
        "scraped_courses", "academic_level_options",
        ["academic_level_option_id"], ["id"], ondelete="RESTRICT",
    )
    # A clean installation has no configured options yet. Seed only that
    # unambiguous empty-table case; never add or replace rows in a configured
    # environment.
    op.execute(sa.text("""
        INSERT INTO academic_level_options (name, sort_order)
        SELECT seed.name, seed.sort_order
        FROM (VALUES
          ('Grade 12th or equivalent', 1),
          ('Bachelor''s degree or equivalent', 3),
          ('Master''s degree or equivalent', 7),
          ('Doctorate / PhD', 8),
          ('Grade 10th or equivalent', 9)
        ) AS seed(name, sort_order)
        WHERE NOT EXISTS (SELECT 1 FROM academic_level_options)
    """))
    # Deliberately exact production names: settings are never created by this
    # migration and installations with missing names remain safely unmapped.
    for name, key in (
        ("Grade 12th or equivalent", "grade_12_equivalent"),
        ("Bachelor's degree or equivalent", "bachelors_equivalent"),
        ("Master's degree or equivalent", "masters_equivalent"),
        ("Doctorate / PhD", "doctorate_phd"),
        ("Grade 10th or equivalent", "grade_10_equivalent"),
    ):
        op.execute(sa.text(
            "UPDATE academic_level_options SET mapping_key=:key "
            "WHERE name=:name AND mapping_key IS NULL"
        ).bindparams(key=key, name=name))
    op.execute(sa.text("""
        DO $$
        BEGIN
          IF (
            SELECT count(*)
            FROM academic_level_options
            WHERE mapping_key IN (
              'grade_12_equivalent', 'bachelors_equivalent',
              'masters_equivalent', 'doctorate_phd', 'grade_10_equivalent'
            )
          ) <> 5 THEN
            RAISE EXCEPTION
              'Required Settings > Academic Levels options are missing; migration aborted';
          END IF;
        END $$;
    """))

    # Map each existing row from its course's production degree level.  The
    # option join naturally leaves rows null where a setting is absent.
    op.execute(sa.text("""
        UPDATE academic_requirements ar
        SET academic_level_option_id = alo.id,
            academic_level = NULL
        FROM courses c
        JOIN academic_level_options alo ON alo.mapping_key = CASE
          WHEN lower(trim(c.degree_level)) IN
            ('master', 'master''s', 'graduate certificate & diploma',
             'graduate certificate', 'graduate diploma')
            THEN 'bachelors_equivalent'
          WHEN lower(trim(c.degree_level)) IN
            ('doctor', 'doctorate', 'doctor/doctorate', 'phd',
             'doctorate/phd', 'doctorate / phd')
            THEN 'masters_equivalent'
          WHEN lower(trim(c.degree_level)) IN
            ('associate degree', 'associate degree or equivalent',
             'certificate & diploma', 'certificate',
             'diploma', 'advanced diploma', 'bachelor', 'bachelor''s')
            THEN 'grade_12_equivalent'
          ELSE NULL END
        WHERE ar.course_id = c.id
    """))
    # Give mapped courses a requirement even when the scrape had no explicit
    # score; do not disturb existing score/type/country values.
    op.execute(sa.text("""
        INSERT INTO academic_requirements
          (course_id, academic_level, academic_level_option_id)
        SELECT c.id, NULL, alo.id
        FROM courses c
        JOIN academic_level_options alo ON alo.mapping_key = CASE
          WHEN lower(trim(c.degree_level)) IN
            ('master', 'master''s', 'graduate certificate & diploma',
             'graduate certificate', 'graduate diploma')
            THEN 'bachelors_equivalent'
          WHEN lower(trim(c.degree_level)) IN
            ('doctor', 'doctorate', 'doctor/doctorate', 'phd',
             'doctorate/phd', 'doctorate / phd')
            THEN 'masters_equivalent'
          WHEN lower(trim(c.degree_level)) IN
            ('associate degree', 'associate degree or equivalent',
             'certificate & diploma', 'certificate',
             'diploma', 'advanced diploma', 'bachelor', 'bachelor''s')
            THEN 'grade_12_equivalent'
          ELSE NULL END
        WHERE NOT EXISTS (
          SELECT 1 FROM academic_requirements ar WHERE ar.course_id = c.id
        )
    """))
    op.execute(sa.text("""
        DO $$
        BEGIN
          IF to_regclass('academic_requirements_backup') IS NOT NULL THEN
            ALTER TABLE academic_requirements_backup
              ADD COLUMN IF NOT EXISTS academic_level_option_id integer;
          END IF;
        END $$;
    """))


def downgrade() -> None:
    op.execute(sa.text("""
        UPDATE academic_requirements ar
        SET academic_level = alo.name
        FROM academic_level_options alo
        WHERE ar.academic_level_option_id = alo.id
    """))
    op.execute(sa.text("""
        UPDATE scraped_courses sc
        SET academic_level = alo.name
        FROM academic_level_options alo
        WHERE sc.academic_level_option_id = alo.id
    """))
    op.drop_constraint(
        "fk_academic_requirements_academic_level_option_id",
        "academic_requirements", type_="foreignkey",
    )
    op.drop_index("ix_academic_requirements_academic_level_option_id",
                  table_name="academic_requirements")
    op.drop_column("academic_requirements", "academic_level_option_id")
    op.drop_constraint(
        "fk_scraped_courses_academic_level_option_id",
        "scraped_courses", type_="foreignkey",
    )
    op.drop_index("ix_scraped_courses_academic_level_option_id",
                  table_name="scraped_courses")
    op.drop_column("scraped_courses", "academic_level_option_id")
    op.drop_index("uq_academic_level_options_mapping_key",
                  table_name="academic_level_options")
    op.drop_column("academic_level_options", "mapping_key")