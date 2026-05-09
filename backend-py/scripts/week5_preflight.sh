#!/usr/bin/env bash
# Week 5 — Pre-flight gates before scaling to next 20 universities.
#
# The original spec uses several columns / tables that DO NOT EXIST in this
# codebase.  This script uses the actual schema:
#   - alert table is `scrape_run_alerts` (NOT `scrape_alerts`); it has
#     `acknowledged` / `acknowledged_at` (NOT `resolved_at`); columns are
#     `rule_id` / `created_at` (NOT `rule_type` / `fired_at`).
#   - `scrape_run_metrics` is a per-(uni,field,method) ledger with `computed_at`
#     (NOT `run_started_at`); per-job ledger lives in `scrape_runtime_jobs`.
#   - `universities` has NO `slug` column; we filter by name pattern instead.
#   - per-uni YAMLs live under `backend-py/scraper_config/unis/`
#     (NOT `backend-py/scraper/unis/`).
#
# Run on prod:
#   cd /root/University-and-Course-data && \
#     sudo -u postgres bash backend-py/scripts/week5_preflight.sh
set -euo pipefail
PSQL="${PSQL:-sudo -u postgres psql -d university_portal -At}"

echo "=========================================================="
echo "GATE 1 — Week 4 unis still healthy (no unack critical alerts)"
echo "=========================================================="
$PSQL -P "format=aligned" -c "
WITH week4_unis AS (
  SELECT DISTINCT j.university_id
  FROM scrape_runtime_jobs j
  WHERE j.started_at > now() - interval '14 days'
    AND j.imported > 0
)
SELECT u.name,
  (SELECT COUNT(*) FROM courses c WHERE c.university_id = u.id) AS production_courses,
  (SELECT MAX(j.started_at) FROM scrape_runtime_jobs j WHERE j.university_id = u.id) AS last_scraped,
  (SELECT COUNT(*) FROM scrape_run_alerts a
     JOIN scrape_runtime_jobs j ON j.runtime_job_id = a.scrape_run_id
     WHERE j.university_id = u.id
       AND a.severity = 'critical' AND a.acknowledged = false) AS active_critical_alerts
FROM universities u
WHERE u.id IN (SELECT university_id FROM week4_unis)
ORDER BY active_critical_alerts DESC, u.name;
"
G1=$($PSQL -c "
SELECT COUNT(*) FROM scrape_run_alerts a
WHERE a.severity = 'critical' AND a.acknowledged = false
  AND a.created_at > now() - interval '14 days';
")
echo "  total unack critical alerts (last 14d): ${G1}"
if [ "$G1" -eq 0 ]; then echo "  GATE 1: PASS"; else echo "  GATE 1: FAIL — investigate before adding more unis"; fi

echo ""
echo "=========================================================="
echo "GATE 2 — Patterns doc updated in last 14d"
echo "=========================================================="
G2=$(git --no-optional-locks log --since='14 days ago' --oneline -- backend-py/docs/uni_onboarding_patterns.md 2>/dev/null | wc -l)
echo "  commits to patterns doc in last 14d: ${G2}"
if [ "$G2" -ge 5 ]; then echo "  GATE 2: PASS"
elif [ "$G2" -ge 1 ]; then echo "  GATE 2: PARTIAL — only ${G2} commits, target is 5+"
else echo "  GATE 2: FAIL — patterns doc untouched, Week 4 patterns not captured"; fi

echo ""
echo "=========================================================="
echo "GATE 3 — Per-uni YAML library size"
echo "=========================================================="
YAML_COUNT=$(find backend-py/scraper_config/unis -maxdepth 1 -name '*.yaml' 2>/dev/null | wc -l)
echo "  per-uni YAMLs present: ${YAML_COUNT}"
if [ "$YAML_COUNT" -ge 7 ]; then echo "  GATE 3: PASS"
else echo "  GATE 3: FAIL — only ${YAML_COUNT} YAMLs, target ≥7"; fi

echo ""
echo "=========================================================="
echo "GATE 4 — Cost projection actioned (manual confirmation)"
echo "=========================================================="
echo "  Run scripts/week4_cost_projection.sql and confirm:"
echo "    1. Median cost/course is < 0.0008 USD"
echo "    2. No uni > 10x median (outliers reviewed and either fixed or accepted)"
echo "    3. Decision recorded in docs/week4_scale_up_log.md cumulative-metrics section"
echo "  This is operator-confirmed, not script-confirmed."

echo ""
echo "=========================================================="
echo "Week 5 readiness — next-20 stub YAMLs present?"
echo "=========================================================="
for slug in macquarie curtin griffith latrobe qut westernsydney adelaide flinders \
            newcastle uow murdoch jcu ecu cdu unisq federation acu csu scu bond; do
  if [ -f "backend-py/scraper_config/unis/${slug}.yaml" ]; then
    printf "  %-15s YAML: YES\n" "$slug"
  else
    printf "  %-15s YAML: missing (uses defaults.yaml)\n" "$slug"
  fi
done

echo ""
echo "Done."
