"""Week 2 P4 — alert dashboard views.

Creates two read-only views that the existing dashboard UI can query
without complex joins:

  * ``v_active_alerts``     — every unresolved alert with university name
                              and human-readable age, ordered critical → info.
  * ``v_university_health`` — one row per university with latest scrape
                              metrics and active alert counts.

Both views map the spec's column names onto the existing schema:

    spec name                        →  existing column
    ─────────────────────────────────────────────────────────────────
    severity                         →  severity   (already matches)
    rule_type                        →  rule_id    (we expose the prefix
                                                    before the first ":"
                                                    as rule_type)
    field_key                        →  derived from rule_id suffix
    title                            →  rule_id   (short human label)
    description                      →  message
    observed_value, baseline_value   →  actual, expected
    fired_at                         →  created_at
    resolved_at                      →  acknowledged_at  (NULL == active)

The metrics view derives fill rates from ``scrape_run_summary`` (Week 2
P1 wide-format table) so the per-uni latest scrape row is a single
``LEFT JOIN LATERAL`` — no per-field aggregation needed.

PROD APPLY (alembic is unusable on prod — see replit.md):

    sudo -u postgres psql -d university_portal <<'SQL'
    -- See ``upgrade()`` body for the literal CREATE OR REPLACE VIEW
    -- statements; they are idempotent and safe to re-apply.
    SQL

NOTE: The views reference ``scrape_run_summary``, ``scrape_run_alerts``
and ``universities``.  All three exist in dev and prod as of W2 P1.
"""
from __future__ import annotations

from alembic import op


# revision identifiers, used by Alembic.
revision = "017_alert_dashboard_views"
down_revision = "016_add_scrape_run_summary"
branch_labels = None
depends_on = None


_V_ACTIVE_ALERTS_SQL = """
CREATE OR REPLACE VIEW v_active_alerts AS
SELECT
    a.id,
    u.name                                   AS university,
    a.severity,
    split_part(a.rule_id, ':', 1)            AS rule_type,
    NULLIF(split_part(a.rule_id, ':', 2), '') AS field_key,
    a.rule_id                                AS title,
    a.message                                AS description,
    to_jsonb(a.actual)                       AS observed_value,
    to_jsonb(a.expected)                     AS baseline_value,
    a.created_at                             AS fired_at,
    AGE(now(), a.created_at)                 AS age,
    a.acknowledged_at                        AS resolved_at
FROM scrape_run_alerts a
JOIN scrape_run_summary s ON s.scrape_run_id = a.scrape_run_id
JOIN universities       u ON u.id = s.university_id
WHERE a.acknowledged = false
ORDER BY
    CASE a.severity
        WHEN 'critical' THEN 1
        WHEN 'warning'  THEN 2
        ELSE                 3
    END,
    a.created_at DESC;
"""


_V_UNIVERSITY_HEALTH_SQL = """
CREATE OR REPLACE VIEW v_university_health AS
SELECT
    u.name                                                  AS university,
    COALESCE(latest.candidates_staged, 0)                   AS latest_staged,
    COALESCE(latest.fill_rate_international_fee, 0)         AS fill_rate_intl_fee,
    COALESCE(latest.fill_rate_ielts_overall, 0)             AS fill_rate_ielts,
    COALESCE(latest.gemini_cost_usd, 0)                     AS latest_cost,
    latest.run_started_at                                   AS last_scraped,
    COALESCE(active_alert_counts.critical, 0)               AS critical_alerts,
    COALESCE(active_alert_counts.warning,  0)               AS warning_alerts,
    COALESCE(active_alert_counts.info,     0)               AS info_alerts,
    CASE
        WHEN COALESCE(active_alert_counts.critical, 0) > 0 THEN 'critical'
        WHEN COALESCE(active_alert_counts.warning,  0) > 0 THEN 'warning'
        ELSE 'healthy'
    END                                                     AS overall_status
FROM universities u
LEFT JOIN LATERAL (
    SELECT *
    FROM scrape_run_summary s
    WHERE s.university_id = u.id
    ORDER BY s.run_started_at DESC NULLS LAST
    LIMIT 1
) latest ON true
LEFT JOIN (
    SELECT
        s.university_id,
        COUNT(*) FILTER (WHERE a.severity = 'critical') AS critical,
        COUNT(*) FILTER (WHERE a.severity = 'warning')  AS warning,
        COUNT(*) FILTER (WHERE a.severity = 'info')     AS info
    FROM scrape_run_alerts a
    JOIN scrape_run_summary s ON s.scrape_run_id = a.scrape_run_id
    WHERE a.acknowledged = false
    GROUP BY s.university_id
) active_alert_counts ON active_alert_counts.university_id = u.id
ORDER BY
    CASE
        WHEN COALESCE(active_alert_counts.critical, 0) > 0 THEN 1
        WHEN COALESCE(active_alert_counts.warning,  0) > 0 THEN 2
        ELSE                                                     3
    END,
    u.name;
"""


def upgrade() -> None:
    op.execute(_V_ACTIVE_ALERTS_SQL)
    op.execute(_V_UNIVERSITY_HEALTH_SQL)


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS v_university_health;")
    op.execute("DROP VIEW IF EXISTS v_active_alerts;")
