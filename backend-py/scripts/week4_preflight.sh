#!/usr/bin/env bash
# Week 4 — Production scale-up pre-flight gates.
#
# The original spec used schema column names that do not exist in this
# codebase.  This script uses the ACTUAL columns from scrape_run_metrics,
# scrape_run_alerts, and scrape_runtime_jobs.
#
# Run on prod:
#
#   cd /root/University-and-Course-data && \
#     sudo -u postgres bash backend-py/scripts/week4_preflight.sh
#
# Each gate prints a PASS / FAIL line at the end.  Do not start Week 4
# scrapes until every gate prints PASS.
set -euo pipefail

PSQL="${PSQL:-sudo -u postgres psql -d university_portal -At}"

echo "=========================================================="
echo "GATE 1 — Sibling-cache provenance has rows in last 7 days"
echo "=========================================================="
G1=$($PSQL -c "
SELECT COUNT(*)
FROM scraped_field_evidence sfe
JOIN scraped_courses sc ON sc.id = sfe.scraped_course_id
WHERE sc.created_at > now() - interval '7 days'
  AND sfe.extraction_method ILIKE '%sibling_cache%';
")
echo "  rows: ${G1}"
if [ "$G1" -gt 0 ]; then echo "  GATE 1: PASS"; else echo "  GATE 1: FAIL — sibling_cache produced no rows"; fi

echo ""
echo "  Detail by uni:"
$PSQL -P "format=aligned" -c "
SELECT u.name,
  COUNT(*) FILTER (WHERE sfe.extraction_method ILIKE '%sibling_cache%') AS backfill_rows,
  COUNT(*) FILTER (WHERE sfe.extraction_method ILIKE '%sibling_cache%' AND sfe.snippet IS NOT NULL) AS with_provenance
FROM scraped_field_evidence sfe
JOIN scraped_courses sc ON sc.id = sfe.scraped_course_id
JOIN universities u ON u.id = sc.university_id
WHERE sc.created_at > now() - interval '7 days'
GROUP BY u.name
HAVING COUNT(*) FILTER (WHERE sfe.extraction_method ILIKE '%sibling_cache%') > 0
ORDER BY backfill_rows DESC LIMIT 25;
" || true

echo ""
echo "=========================================================="
echo "GATE 2 — Alert evaluator fired at least 1 critical in 7d"
echo "=========================================================="
# Schema fix: scrape_run_alerts has rule_id (not rule_type) and created_at (not fired_at)
G2=$($PSQL -c "
SELECT COUNT(*) FROM scrape_run_alerts
WHERE created_at > now() - interval '7 days' AND severity = 'critical';
")
echo "  critical alerts: ${G2}"
if [ "$G2" -gt 0 ]; then echo "  GATE 2: PASS"; else echo "  GATE 2: FAIL — alert evaluator silent"; fi

echo ""
echo "  Detail:"
$PSQL -P "format=aligned" -c "
SELECT severity, rule_id, COUNT(*)
FROM scrape_run_alerts
WHERE created_at > now() - interval '7 days'
GROUP BY 1,2 ORDER BY 1,3 DESC LIMIT 30;
" || true

echo ""
echo "=========================================================="
echo "GATE 3 — Week 3 cost reduction visible (per-job ledger)"
echo "=========================================================="
# Schema fix: scrape_run_metrics is a per-(uni,field,method) ledger, NOT one
# row per scrape job.  Job-level cost lives in scrape_runtime_jobs:
# total_gemini_cost_usd + the 'staged'/'discovered'/'skipped' counters.
G3=$($PSQL -P "format=aligned" -c "
WITH per_job AS (
  SELECT
    j.runtime_job_id,
    j.university_id,
    j.started_at,
    j.total_found, j.imported,
    j.total_gemini_cost_usd,
    CASE WHEN j.imported > 0
         THEN (j.total_gemini_cost_usd / j.imported)
         ELSE NULL END AS cost_per_course
  FROM scrape_runtime_jobs j
  WHERE j.started_at > now() - interval '14 days'
    AND j.imported > 0
)
SELECT u.name,
       COUNT(*) AS runs,
       ROUND(AVG(cost_per_course)::numeric, 6) AS avg_cost_per_course,
       MIN(ROUND(cost_per_course::numeric, 6)) AS best_run
FROM per_job p
JOIN universities u ON u.id = p.university_id
GROUP BY u.name
ORDER BY avg_cost_per_course ASC NULLS LAST
LIMIT 25;
")
echo "$G3"

UNDER=$($PSQL -c "
WITH per_job AS (
  SELECT j.university_id, j.total_gemini_cost_usd / NULLIF(j.imported,0) AS cpc
  FROM scrape_runtime_jobs j
  WHERE j.started_at > now() - interval '14 days' AND j.imported > 0
)
SELECT COUNT(*) FROM (
  SELECT university_id FROM per_job
  GROUP BY university_id HAVING AVG(cpc) < 0.0004
) x;
")
echo "  unis with avg cost/course < 0.0004 (target): ${UNDER}"
if [ "$UNDER" -gt 0 ]; then echo "  GATE 3: PASS"; else echo "  GATE 3: FAIL — Week 3 skip rule not visible in cost data"; fi

echo ""
echo "=========================================================="
echo "GATE 4 — Regression baselines fresh (within 14 days)"
echo "=========================================================="
BASE_DIR="${BASELINES_DIR:-backend-py/baselines}"
if [ -d "$BASE_DIR" ]; then
  STALE=$(find "$BASE_DIR" -maxdepth 1 -name '*.json' -mtime +14 | wc -l)
  TOTAL=$(find "$BASE_DIR" -maxdepth 1 -name '*.json' | wc -l)
  echo "  baseline files: $TOTAL  | stale (>14d): $STALE"
  if [ "$TOTAL" -gt 0 ] && [ "$STALE" -eq 0 ]; then
    echo "  GATE 4: PASS"
  else
    echo "  GATE 4: FAIL — refresh via scripts/capture_baseline.py"
  fi
else
  echo "  GATE 4: FAIL — $BASE_DIR not present"
fi

echo ""
echo "=========================================================="
echo "TOP-10 readiness — per-uni YAML present?"
echo "=========================================================="
for slug in monash unimelb usyd unsw uq rmit deakin uts anu uwa; do
  if [ -f "backend-py/scraper_config/unis/${slug}.yaml" ]; then
    printf "  %-10s YAML: YES\n" "$slug"
  else
    printf "  %-10s YAML: missing (defaults.yaml will be used)\n" "$slug"
  fi
done

echo ""
echo "Done.  Re-run after fixing any FAIL gates."
