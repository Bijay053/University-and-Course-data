#!/usr/bin/env bash
# ============================================================
# Task 259 — UWE Bristol & Kingston: prod scrape + timing verify
#
# Run on production droplet: /root/University-and-Course-data
#
#   bash backend-py/scripts/task259_run_uwe_kingston_prod.sh
#
# What this script does:
#   1. Resolves university IDs (prod IDs differ from dev)
#   2. Triggers scrapes via the FastAPI POST endpoint
#   3. Polls until both jobs complete (up to 35 min each)
#   4. Runs the timing verification SQL and prints PASS/FAIL
#   5. If Kingston misses ≤30 min target, bumps max_parallel_fetch
#      from 2 → 3 and offers to re-run
#
# Timing targets (from task spec):
#   UWE Bristol  : ≤25 min, ≥600 courses staged
#   Kingston     : ≤30 min, ≥380 courses staged
# ============================================================
set -euo pipefail

PROD_API_BASE="${PROD_API_BASE:-http://127.0.0.1:8000}"
PSQL="sudo -u postgres psql -d university_portal"
REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
KINGSTON_YAML="$REPO_ROOT/backend-py/scraper_config/unis/kingston.yaml"
VERIFY_SQL="$REPO_ROOT/backend-py/scripts/task255_uwe_kingston_timing_verify.sql"

echo "=== Task 259: UWE Bristol & Kingston production scrape ==="
echo "Repo root : $REPO_ROOT"
echo "API base  : $PROD_API_BASE"
echo ""

# ─── Step 0: Resolve university IDs ───────────────────────────────────────────
echo "--- STEP 0: Resolving university IDs ---"
IDS_JSON=$($PSQL -t -A -c "
SELECT json_agg(row_to_json(r))
FROM (
    SELECT id, name
    FROM universities
    WHERE name ILIKE '%west of england%'
       OR name ILIKE '%uwe%'
       OR name ILIKE '%kingston%'
    ORDER BY name
) r;" 2>/dev/null)

echo "Matched universities:"
echo "$IDS_JSON" | python3 -c "
import json, sys
rows = json.load(sys.stdin) or []
for r in rows:
    print(f\"  id={r['id']}  name={r['name']}\")
"

# Let the operator confirm or override
read -rp "Enter UWE Bristol university_id: " UWE_ID
read -rp "Enter Kingston university_id   : " KINGSTON_ID

echo ""
echo "Using: UWE id=$UWE_ID  |  Kingston id=$KINGSTON_ID"
echo ""

# ─── Helper: trigger a scrape via FastAPI ─────────────────────────────────────
trigger_scrape() {
    local UNI_ID="$1"
    local UNI_LABEL="$2"
    echo "--- Triggering $UNI_LABEL (uni_id=$UNI_ID) scrape ---"
    RESPONSE=$(curl -s -X POST "$PROD_API_BASE/api/scraping/trigger" \
        -H "Content-Type: application/json" \
        -d "{\"university_id\": $UNI_ID}" \
        --cookie-jar /tmp/scrape_cookie_jar \
        --cookie /tmp/scrape_cookie_jar \
        -w "\nHTTP_STATUS:%{http_code}")
    HTTP_CODE=$(echo "$RESPONSE" | grep "HTTP_STATUS:" | cut -d: -f2)
    BODY=$(echo "$RESPONSE" | grep -v "HTTP_STATUS:")
    echo "  HTTP $HTTP_CODE — $BODY"
    if [[ "$HTTP_CODE" != "200" && "$HTTP_CODE" != "202" ]]; then
        echo "  WARNING: Unexpected status $HTTP_CODE. Check that the session cookie is valid."
        echo "  Continuing — poll the DB to detect if a job started anyway."
    fi
}

# ─── Helper: poll until job done ──────────────────────────────────────────────
poll_until_done() {
    local UNI_ID="$1"
    local UNI_LABEL="$2"
    local MAX_WAIT_MIN="${3:-35}"
    local INTERVAL_S=30

    echo "--- Polling $UNI_LABEL (uni_id=$UNI_ID) — max wait ${MAX_WAIT_MIN} min ---"
    local ELAPSED=0
    local MAX_WAIT_S=$(( MAX_WAIT_MIN * 60 ))

    while true; do
        STATUS=$($PSQL -t -A -c "
SELECT COALESCE(status, 'not_started')
FROM scrape_runtime_jobs
WHERE university_id = $UNI_ID
  AND job_type = 'scrape'
ORDER BY started_at DESC
LIMIT 1;" 2>/dev/null | tr -d ' ')

        IMPORTED=$($PSQL -t -A -c "
SELECT COALESCE(imported::text, '?')
FROM scrape_runtime_jobs
WHERE university_id = $UNI_ID
  AND job_type = 'scrape'
ORDER BY started_at DESC
LIMIT 1;" 2>/dev/null | tr -d ' ')

        echo "  [$(date +%H:%M:%S)] $UNI_LABEL — status=$STATUS  imported=$IMPORTED  elapsed=${ELAPSED}s"

        if [[ "$STATUS" == "done" || "$STATUS" == "error" || "$STATUS" == "failed" ]]; then
            echo "  $UNI_LABEL job finished with status=$STATUS"
            return 0
        fi

        if (( ELAPSED >= MAX_WAIT_S )); then
            echo "  WARNING: $UNI_LABEL timed out after ${MAX_WAIT_MIN} min — job may still be running"
            return 1
        fi

        sleep "$INTERVAL_S"
        ELAPSED=$(( ELAPSED + INTERVAL_S ))
    done
}

# ─── Step 1: Trigger both scrapes ─────────────────────────────────────────────
echo "=== STEP 1: Triggering scrapes ==="
echo ""
echo "NOTE: If the API uses session authentication, log in via the portal"
echo "      first, then export the session cookie to /tmp/scrape_cookie_jar."
echo "      Alternatively trigger scrapes via the portal UI and skip to Step 2."
echo ""
read -rp "Trigger scrapes automatically via API? [y/N]: " DO_TRIGGER
if [[ "${DO_TRIGGER,,}" == "y" ]]; then
    trigger_scrape "$UWE_ID" "UWE Bristol"
    echo ""
    trigger_scrape "$KINGSTON_ID" "Kingston"
    echo ""
else
    echo "Skipping API trigger — trigger scrapes manually via the portal UI now."
    echo "Then press Enter here to begin polling..."
    read -r
fi

# ─── Step 2: Poll UWE until done ──────────────────────────────────────────────
echo ""
echo "=== STEP 2: Waiting for UWE Bristol to complete (max 28 min) ==="
poll_until_done "$UWE_ID" "UWE Bristol" 28

# ─── Step 3: Poll Kingston until done ─────────────────────────────────────────
echo ""
echo "=== STEP 3: Waiting for Kingston to complete (max 33 min) ==="
poll_until_done "$KINGSTON_ID" "Kingston" 33

# ─── Step 4: Run timing verification SQL ──────────────────────────────────────
echo ""
echo "=== STEP 4: Running timing verification SQL ==="
$PSQL \
    -v "uwe_id=$UWE_ID" \
    -v "kingston_id=$KINGSTON_ID" \
    -f "$VERIFY_SQL"

# ─── Step 5: Kingston 429 / timing fallback ───────────────────────────────────
echo ""
echo "=== STEP 5: Kingston timing fallback check ==="

KINGSTON_RESULT=$($PSQL -t -A -c "
SELECT
    EXTRACT(EPOCH FROM (completed_at - started_at))::int / 60 AS duration_min,
    imported,
    CASE
        WHEN EXTRACT(EPOCH FROM (completed_at - started_at)) <= 1800
         AND imported >= 380 THEN 'PASS'
        ELSE 'FAIL'
    END AS result
FROM scrape_runtime_jobs
WHERE university_id = $KINGSTON_ID
  AND job_type = 'scrape'
  AND status = 'done'
ORDER BY started_at DESC
LIMIT 1;" 2>/dev/null | tr -d ' ')

KINGSTON_RESULT_LABEL=$(echo "$KINGSTON_RESULT" | awk -F'|' '{print $3}')
KINGSTON_DURATION=$(echo "$KINGSTON_RESULT" | awk -F'|' '{print $1}')
KINGSTON_IMPORTED=$(echo "$KINGSTON_RESULT" | awk -F'|' '{print $2}')

echo "Kingston result: $KINGSTON_RESULT_LABEL (${KINGSTON_DURATION} min, ${KINGSTON_IMPORTED} imported)"

if [[ "$KINGSTON_RESULT_LABEL" == "FAIL" ]]; then
    echo ""
    echo "Kingston FAILED timing target (≤30 min, ≥380 courses)."
    echo ""
    echo "Per task spec: max_parallel_fetch can be raised from 2 → 3 for extraction"
    echo "(the 429 warning in the YAML applies to BFS listing-page fetching, not"
    echo "per-course extraction — cffi impersonation is less aggressive at 3 concurrent)."
    echo ""

    CURRENT_PARALLEL=$(grep 'max_parallel_fetch' "$KINGSTON_YAML" | awk '{print $2}')
    echo "Current max_parallel_fetch = $CURRENT_PARALLEL in $KINGSTON_YAML"

    if [[ "$CURRENT_PARALLEL" == "2" ]]; then
        read -rp "Bump max_parallel_fetch from 2 → 3 and re-run Kingston? [y/N]: " DO_BUMP
        if [[ "${DO_BUMP,,}" == "y" ]]; then
            # Update the YAML in place
            sed -i 's/^  max_parallel_fetch: 2$/  max_parallel_fetch: 3/' "$KINGSTON_YAML"
            echo "Updated $KINGSTON_YAML — max_parallel_fetch now 3"
            echo ""
            echo "Triggering Kingston re-run..."
            if [[ "${DO_TRIGGER,,}" == "y" ]]; then
                trigger_scrape "$KINGSTON_ID" "Kingston (re-run)"
            else
                echo "Trigger Kingston re-run via portal UI, then press Enter..."
                read -r
            fi
            echo ""
            echo "=== Waiting for Kingston re-run (max 33 min) ==="
            poll_until_done "$KINGSTON_ID" "Kingston (re-run)" 33
            echo ""
            echo "=== Re-run verification ==="
            $PSQL \
                -v "uwe_id=$UWE_ID" \
                -v "kingston_id=$KINGSTON_ID" \
                -f "$VERIFY_SQL"
        fi
    else
        echo "max_parallel_fetch is already $CURRENT_PARALLEL — no change made."
    fi
else
    echo "Kingston PASSED. No YAML changes needed."
fi

# ─── Summary ──────────────────────────────────────────────────────────────────
echo ""
echo "=== DONE ==="
echo "Verify results above. If both show PASS in STEP 2 of the SQL output,"
echo "Task 259 timing targets are confirmed met."
echo ""
echo "If any scrape shows FAIL:"
echo "  UWE FAIL  → check scrape_run_alerts for the job; may need seed_urls updated"
echo "              for a new academic year (currently seeded with e=2026)."
echo "  Kingston FAIL → if max_parallel_fetch was already bumped to 3 and still"
echo "              failing, investigate 429 rate (error_rate_pct in STEP 6 of SQL)."
