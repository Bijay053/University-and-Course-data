"""Week 3 P5B — CRICOS coverage view for AU universities.

Provides a one-row-per-AU-uni view that satisfies the spec verification
SQL.  ``courses.cricos_code`` does NOT exist (only ``scraped_courses``
holds it) so the view queries the staging table.

  * total_staged          — pending scraped_courses for that uni
  * has_cricos            — count with non-null cricos_code
  * cricos_coverage_pct   — has_cricos / total_staged * 100
  * enriched_via_pdf      — count whose evidence shows
                            extraction_method matching ``%cricos_match%``

Operators can sort by coverage_pct ascending to spot extractor blind
spots (universities where the regex should match but doesn't).

PROD APPLY:

    sudo -u postgres psql -d university_portal <<'SQL'
    -- See ``upgrade()`` for the literal CREATE OR REPLACE VIEW statement
    SQL
"""
from __future__ import annotations

from alembic import op


revision = "018_cricos_coverage_view"
down_revision = "017_alert_dashboard_views"
branch_labels = None
depends_on = None


_V_CRICOS_COVERAGE_AU_SQL = """
CREATE OR REPLACE VIEW v_cricos_coverage_au AS
SELECT
    u.name                                                          AS university,
    u.country,
    COUNT(*)                                                        AS total_staged,
    COUNT(*) FILTER (WHERE sc.cricos_code IS NOT NULL)              AS has_cricos,
    ROUND(
        COUNT(*) FILTER (WHERE sc.cricos_code IS NOT NULL)::numeric
            / NULLIF(COUNT(*), 0) * 100,
        1
    )                                                               AS cricos_coverage_pct,
    COUNT(*) FILTER (
        WHERE EXISTS (
            SELECT 1 FROM scraped_field_evidence sfe
            WHERE sfe.scraped_course_id = sc.id
              AND sfe.extraction_method LIKE '%cricos_match%'
        )
    )                                                               AS enriched_via_pdf
FROM scraped_courses sc
JOIN universities    u  ON u.id = sc.university_id
WHERE u.country IN ('Australia', 'AU')
  AND sc.status = 'pending'
GROUP BY u.name, u.country
ORDER BY cricos_coverage_pct DESC NULLS LAST, u.name;
"""


def upgrade() -> None:
    op.execute(_V_CRICOS_COVERAGE_AU_SQL)


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS v_cricos_coverage_au;")
