"""Move dated catalogue review history into an indexed append-only table."""

from alembic import op


revision = "385_dated_review_history"
down_revision = "384_requirement_status"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        CREATE TABLE IF NOT EXISTS dated_catalogue_review_history (
            id BIGSERIAL PRIMARY KEY,
            scraped_course_id INTEGER NOT NULL
                REFERENCES scraped_courses(id) ON DELETE CASCADE,
            revision INTEGER NOT NULL,
            event JSONB NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT uq_dated_catalogue_review_history_row_revision
                UNIQUE (scraped_course_id, revision)
        )
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS ix_dated_catalogue_review_history_row_revision_desc
        ON dated_catalogue_review_history (scraped_course_id, revision DESC)
    """)
    op.execute("""
        INSERT INTO dated_catalogue_review_history
            (scraped_course_id, revision, event)
        SELECT
            sc.id,
            CASE
                WHEN COALESCE(
                    (sc.extraction_method #>> '{dated_catalogue_review,revision}')::integer,
                    0
                ) >= jsonb_array_length(
                    sc.extraction_method #> '{dated_catalogue_review,history}'
                )
                THEN (
                    (sc.extraction_method #>> '{dated_catalogue_review,revision}')::integer
                    - jsonb_array_length(
                        sc.extraction_method #> '{dated_catalogue_review,history}'
                    )
                    + history_item.ordinality
                )::integer
                ELSE history_item.ordinality::integer
            END,
            history_item.event
        FROM scraped_courses AS sc
        CROSS JOIN LATERAL jsonb_array_elements(
            CASE
                WHEN jsonb_typeof(sc.extraction_method #> '{dated_catalogue_review,history}') = 'array'
                THEN sc.extraction_method #> '{dated_catalogue_review,history}'
                ELSE '[]'::jsonb
            END
        ) WITH ORDINALITY AS history_item(event, ordinality)
        ON CONFLICT (scraped_course_id, revision) DO NOTHING
    """)
    op.execute("""
        UPDATE scraped_courses
        SET extraction_method = jsonb_set(
            extraction_method,
            '{dated_catalogue_review}',
            (extraction_method -> 'dated_catalogue_review') - 'history'
        )
        WHERE jsonb_typeof(extraction_method -> 'dated_catalogue_review') = 'object'
          AND (extraction_method -> 'dated_catalogue_review') ? 'history'
    """)


def downgrade():
    op.execute("""
        UPDATE scraped_courses AS sc
        SET extraction_method = jsonb_set(
            COALESCE(sc.extraction_method, '{}'::jsonb),
            '{dated_catalogue_review,history}',
            COALESCE((
                SELECT jsonb_agg(h.event ORDER BY h.revision)
                FROM dated_catalogue_review_history AS h
                WHERE h.scraped_course_id = sc.id
            ), '[]'::jsonb),
            true
        )
        WHERE EXISTS (
            SELECT 1
            FROM dated_catalogue_review_history AS h
            WHERE h.scraped_course_id = sc.id
        )
    """)
    op.drop_table("dated_catalogue_review_history")