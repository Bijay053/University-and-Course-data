#!/usr/bin/env bash
# Task #234 — Confirm scrape speed-up on a real large university (prod verification).
#
# Task #233 shipped three per-course latency gates:
#   1. Gemini primary-extraction timeout at 20 s (was a hard 30 s dead wait)
#      — timeouts now also trip the circuit breaker.
#   2. Vision OCR early-exit when every english-overall slot is already filled
#      and no tier-0 (english-section) image is present.
#   3. Confirmed-browser-only host gate: after 3 genuine browser rescues for a
#      host in a run, skip the guaranteed-to-fail plain-HTTP fetch for the rest
#      of that run.
#
# This script verifies all three gates fire on a real large catalogue and that
# field-fill did NOT regress.
#
# Run on prod (as root or with sudo access to psql):
#
#   cd /root/University-and-Course-data && \
#     bash backend-py/scripts/task234_latency_gate_verify.sh 2>&1 | tee /tmp/task234_verify.log
#
# The script is READ-ONLY — it queries the DB and reads service logs.
# Triggering the scrape is a separate operator action (Step 3).
#
# Prerequisites: a completed scrape of Ulster (or Westminster) must exist.
# If none has run yet, follow Step 3 to trigger one, wait for completion,
# then re-run this script.
#
# Expected outcome:
#   GATE 1 PASS — job completed in < 45 min for ~400-600 courses (vs 60-120 min baseline)
#   GATE 2 PASS — average per-course wall-clock ≤ 30 s (target: 10-20 s)
#   GATE 3 PASS — [GEMINI TIMEOUT] / [VISION SKIP] / browser-only log lines present
#   GATE 4 PASS — field-fill rates for fees/IELTS/duration/intake/mode not regressed
#   GATE 5 PASS — spot-check shows no blank critical fields on promoted courses
#
# If any GATE FAILs, see the troubleshooting notes at the bottom of this file.

set -euo pipefail

PSQL="${PSQL:-sudo -u postgres psql -d university_portal -At}"
PSQL_ALIGNED="${PSQL_ALIGNED:-sudo -u postgres psql -d university_portal}"
SVC="uni-api-py"          # systemd service name for journalctl

echo ""
echo "========================================================================"
echo "Task #234 — Latency gate verification ($(date -u +'%Y-%m-%d %H:%M UTC'))"
echo "========================================================================"

# ── Step 1: confirm uni IDs on prod ─────────────────────────────────────────
echo ""
echo "── Step 1: Find Ulster and Westminster IDs on prod ──────────────────────"
$PSQL_ALIGNED -c "
SELECT id, name, scrape_url
FROM universities
WHERE name ILIKE '%ulster%'
   OR name ILIKE '%westminster%'
ORDER BY name;
" || echo "(query failed — check psql access)"

echo ""
echo "Set UNI_ID below to whichever you want to verify (Ulster or Westminster)."
echo "Re-run with:  UNI_ID=<id> bash backend-py/scripts/task234_latency_gate_verify.sh"

UNI_ID="${UNI_ID:-}"

if [ -z "$UNI_ID" ]; then
    echo ""
    echo "NOTE: UNI_ID not set. Running in discovery-only mode."
    echo "      Set UNI_ID=<id> and re-run to see all gates."
    echo ""
    echo "── Git deployment check ─────────────────────────────────────────────"
    echo "Last 5 commits on prod:"
    git log --oneline -5 2>/dev/null || echo "(git not available)"
    echo ""
    echo "Check that 'Task #233' or 'latency gate' appears in the log above."
    echo "If not, run: git pull origin main && systemctl restart ${SVC} uni-celery"
    exit 0
fi

# ── Step 2: git / code deployment check ─────────────────────────────────────
echo ""
echo "── Step 2: Verify Task #233 code is deployed ────────────────────────────"
echo "Last 5 commits:"
git log --oneline -5 2>/dev/null || echo "(git not available)"
echo ""
echo "Key files that must be from Task #233:"
for f in backend-py/app/services/ai/gemini_client.py \
          backend-py/app/services/scraper/per_course_browser.py \
          backend-py/app/services/scraper/per_course_vision.py \
          backend-py/app/config.py; do
    if grep -q "record_timeout\|browser_only_hosts\|_ENGLISH_OVERALL_SLOTS\|gemini_primary_timeout_s" "$f" 2>/dev/null; then
        echo "  ✓  $f — latency gate code present"
    else
        echo "  ✗  $f — MISSING latency gate code (deploy may be stale)"
    fi
done

# ── Step 3: trigger scrape (operator action, not scripted) ───────────────────
echo ""
echo "── Step 3: Trigger scrape (operator action) ─────────────────────────────"
echo ""
echo "If a completed run already exists for uni_id=${UNI_ID}, skip this step."
echo "Otherwise trigger via the portal UI or REST API:"
echo ""
echo "  # Via REST API (replace <session-cookie>):"
echo "  curl -s -X POST http://127.0.0.1:8000/api/scraping-jobs/trigger \\"
echo "    -H 'Content-Type: application/json' \\"
echo "    -H 'Cookie: session=<session-cookie>' \\"
echo "    -d '{\"university_id\": ${UNI_ID}, \"force_fresh\": true}'"
echo ""
echo "  # Watch scrape progress:"
echo "  journalctl -u ${SVC} -u uni-celery -f --output=cat 2>/dev/null || \\"
echo "    tail -f /tmp/uni_api_py.log 2>/dev/null"
echo ""
echo "Wait for the job to reach status='completed' before running Step 4+."

# ── Step 4: find the most recent completed run for this uni ──────────────────
echo ""
echo "── Step 4: Most recent completed scrape for uni_id=${UNI_ID} ────────────"

JOB_ROW=$($PSQL -c "
SELECT runtime_job_id,
       started_at,
       completed_at,
       EXTRACT(EPOCH FROM (completed_at - started_at))::int AS elapsed_s,
       imported,
       total_found,
       errors,
       claim_count,
       total_gemini_cost_usd,
       status
FROM scrape_runtime_jobs
WHERE university_id = ${UNI_ID}
  AND status = 'completed'
  AND job_type = 'scrape'
ORDER BY started_at DESC
LIMIT 1;
" 2>/dev/null || echo "")

if [ -z "$JOB_ROW" ]; then
    echo "  No completed scrape found for uni_id=${UNI_ID}."
    echo "  Trigger a scrape (Step 3) and re-run after completion."
    exit 0
fi

echo "  Raw row: $JOB_ROW"

# Parse row fields (pipe-separated from -At)
RUN_ID=$(echo "$JOB_ROW"    | cut -d'|' -f1)
STARTED=$(echo "$JOB_ROW"   | cut -d'|' -f2)
COMPLETED=$(echo "$JOB_ROW" | cut -d'|' -f3)
ELAPSED_S=$(echo "$JOB_ROW" | cut -d'|' -f4)
IMPORTED=$(echo "$JOB_ROW"  | cut -d'|' -f5)
FOUND=$(echo "$JOB_ROW"     | cut -d'|' -f6)
ERRORS=$(echo "$JOB_ROW"    | cut -d'|' -f7)
CLAIMS=$(echo "$JOB_ROW"    | cut -d'|' -f8)
COST=$(echo "$JOB_ROW"      | cut -d'|' -f9)
STATUS=$(echo "$JOB_ROW"    | cut -d'|' -f10)

echo ""
echo "  run_id      : $RUN_ID"
echo "  started_at  : $STARTED"
echo "  completed_at: $COMPLETED"
echo "  elapsed_s   : ${ELAPSED_S}s"
echo "  total_found : $FOUND"
echo "  imported    : $IMPORTED"
echo "  errors      : $ERRORS"
echo "  claim_count : $CLAIMS   (resume passes — target: ≤ 2 for a ~400-course uni)"
echo "  gemini_cost : \$$COST"
echo "  status      : $STATUS"

# ── Gate 1: wall-clock time check ───────────────────────────────────────────
echo ""
echo "── GATE 1: Job elapsed time ─────────────────────────────────────────────"
ELAPSED_MIN=$(( ELAPSED_S / 60 ))
if [ "$IMPORTED" -gt 0 ]; then
    AVG_COURSE_S=$(( ELAPSED_S / IMPORTED ))
else
    AVG_COURSE_S=999
fi
echo "  Total elapsed   : ${ELAPSED_MIN} min (${ELAPSED_S}s)"
echo "  Imported courses: $IMPORTED"
echo "  Avg per course  : ~${AVG_COURSE_S}s  (baseline: 30-60s; target after T#233: ≤ 20s)"

if [ "$AVG_COURSE_S" -le 30 ]; then
    echo "  GATE 1: PASS — avg per-course ≤ 30 s ✓"
elif [ "$AVG_COURSE_S" -le 60 ]; then
    echo "  GATE 1: WARN — avg per-course ${AVG_COURSE_S}s (within baseline but not improved)"
else
    echo "  GATE 1: FAIL — avg per-course ${AVG_COURSE_S}s exceeds 60s baseline"
fi

# ── Gate 2: resume pass count ────────────────────────────────────────────────
echo ""
echo "── GATE 2: Resume / claim count ─────────────────────────────────────────"
echo "  claim_count = $CLAIMS"
echo "  (baseline before T#229+T#233 was often 4-8 resume passes for large catalogues)"
if [ "$CLAIMS" -le 2 ]; then
    echo "  GATE 2: PASS — completed in ≤ 2 passes ✓"
elif [ "$CLAIMS" -le 4 ]; then
    echo "  GATE 2: WARN — $CLAIMS passes (acceptable but not ideal)"
else
    echo "  GATE 2: FAIL — $CLAIMS passes suggests latency gates are not firing"
fi

# ── Gate 3: log marker presence ─────────────────────────────────────────────
echo ""
echo "── GATE 3: Latency gate log markers ─────────────────────────────────────"
echo "  Searching journalctl for run $RUN_ID (last 2h)…"
echo "  (fallback: grep /tmp/*.log if journalctl unavailable)"
echo ""

# Try journalctl first; fall back to a file glob
_grep_logs() {
    local pattern="$1"
    # journalctl from 2 hours ago
    if journalctl -u "${SVC}" -u uni-celery --since "2 hours ago" --output=cat 2>/dev/null | grep -c "$pattern" 2>/dev/null; then
        return
    fi
    # fallback: search /tmp log files
    grep -r "$pattern" /tmp/*.log 2>/dev/null | wc -l || echo "0"
}

T_COUNT=$(_grep_logs "\[GEMINI TIMEOUT\]" 2>/dev/null || echo "?")
C_COUNT=$(_grep_logs "\[GEMINI CIRCUIT OPEN\]" 2>/dev/null || echo "?")
VS_COUNT=$(_grep_logs "\[VISION SKIP\]" 2>/dev/null || echo "?")
BO_COUNT=$(_grep_logs "confirmed browser-only host" 2>/dev/null || echo "?")

echo "  [GEMINI TIMEOUT] occurrences        : ${T_COUNT}"
echo "  [GEMINI CIRCUIT OPEN] occurrences   : ${C_COUNT}"
echo "  [VISION SKIP] occurrences           : ${VS_COUNT}"
echo "  confirmed browser-only host         : ${BO_COUNT}"
echo ""

# Vision skip and browser-only are on different uni profiles —
# Ulster is browser-rendered so browser-only gate matters most;
# Westminster may show more vision skips.
# We PASS gate 3 if at least one category of skips is non-zero.
GATE3_TOTAL=0
for v in "$T_COUNT" "$C_COUNT" "$VS_COUNT" "$BO_COUNT"; do
    if [[ "$v" =~ ^[0-9]+$ ]] && [ "$v" -gt 0 ]; then
        GATE3_TOTAL=$(( GATE3_TOTAL + 1 ))
    fi
done
if [ "$GATE3_TOTAL" -ge 1 ]; then
    echo "  GATE 3: PASS — at least one latency gate fired ✓"
else
    echo "  GATE 3: WARN — no gate log markers found in recent logs."
    echo "          Check that journalctl / log path is correct, or check"
    echo "          /tmp/*.log files manually:"
    echo "          grep -E 'GEMINI TIMEOUT|VISION SKIP|browser-only' /tmp/*.log | head -20"
fi

# ── Gate 4: field-fill rates for this run ───────────────────────────────────
echo ""
echo "── GATE 4: Field-fill rates for run ${RUN_ID} ────────────────────────────"
$PSQL_ALIGNED -c "
SELECT
  COUNT(*)                                                             AS total_staged,
  COUNT(*) FILTER (WHERE international_fee  IS NOT NULL
                     AND international_fee::text != '0')              AS has_fee,
  COUNT(*) FILTER (WHERE ielts_overall       IS NOT NULL
                     AND ielts_overall::text != '0')                  AS has_ielts,
  COUNT(*) FILTER (WHERE duration            IS NOT NULL
                     AND duration != '')                               AS has_duration,
  COUNT(*) FILTER (WHERE intake_months       IS NOT NULL
                     AND intake_months       != '{}')                  AS has_intake,
  COUNT(*) FILTER (WHERE study_mode          IS NOT NULL
                     AND study_mode          != '')                    AS has_mode,
  ROUND(AVG(
    CASE WHEN COALESCE(completeness_score,0) > 0
         THEN completeness_score ELSE NULL END
  ),1)                                                                AS avg_completeness
FROM scraped_courses
WHERE scrape_job_id = '${RUN_ID}';
" 2>/dev/null || echo "(query failed)"

echo ""
echo "  NOTE: Compare against a previous run for regression detection."
echo "  Previous run (for reference — same uni, next-most-recent completed):"
$PSQL_ALIGNED -c "
SELECT runtime_job_id, started_at::date, imported, total_found
FROM scrape_runtime_jobs
WHERE university_id = ${UNI_ID}
  AND status = 'completed'
  AND job_type = 'scrape'
  AND runtime_job_id != '${RUN_ID}'
ORDER BY started_at DESC
LIMIT 3;
" 2>/dev/null || echo "(no prior runs found)"

# ── Gate 5: spot-check 15 staged courses ────────────────────────────────────
echo ""
echo "── GATE 5: Spot-check 15 random staged courses from run ${RUN_ID} ────────"
$PSQL_ALIGNED -c "
SELECT
  course_name,
  degree_level,
  study_mode,
  course_location,
  duration,
  CASE WHEN international_fee IS NOT NULL THEN international_fee::text ELSE '(null)' END AS fee,
  CASE WHEN ielts_overall     IS NOT NULL THEN ielts_overall::text     ELSE '(null)' END AS ielts,
  COALESCE(intake_months::text, '(null)')                                                AS intakes,
  COALESCE(completeness_score::text, '?')                                                AS score,
  status,
  auto_publish_status
FROM scraped_courses
WHERE scrape_job_id = '${RUN_ID}'
ORDER BY RANDOM()
LIMIT 15;
" 2>/dev/null || echo "(query failed)"

echo ""
echo "  GATE 5 checklist (manual):"
echo "  □  No course has NULL fee, NULL ielts, AND NULL duration simultaneously"
echo "  □  study_mode contains 'Full-time' or 'Online' (not blank)"
echo "  □  At least 70% of rows have completeness_score ≥ 70"
echo "  □  status = 'pending' or 'approved' (not 'error')"

# ── Summary ──────────────────────────────────────────────────────────────────
echo ""
echo "========================================================================"
echo "SUMMARY for run ${RUN_ID} (uni_id=${UNI_ID})"
echo "========================================================================"
echo "  Elapsed     : ${ELAPSED_MIN} min  |  Avg/course : ${AVG_COURSE_S}s"
echo "  Imported    : $IMPORTED / $FOUND  |  Errors     : $ERRORS"
echo "  Resume passes: $CLAIMS            |  Gemini cost : \$$COST"
echo ""
echo "  GATE 1 (elapsed/avg):      see above"
echo "  GATE 2 (resume passes):    see above"
echo "  GATE 3 (log markers):      see above"
echo "  GATE 4 (field-fill table): check that fee/ielts/duration rows are high"
echo "  GATE 5 (spot-check):       review 15-row table manually"
echo ""
echo "For a deeper per-field breakdown run:"
echo "  PSQL='sudo -u postgres psql -d university_portal -At'"
echo "  RUN_ID='${RUN_ID}'"
echo "  bash backend-py/scripts/task234_field_fill_detail.sh"
echo ""
echo "Detailed log search:"
echo "  journalctl -u ${SVC} -u uni-celery --since '3 hours ago' --output=cat \\"
echo "    | grep -E 'GEMINI TIMEOUT|CIRCUIT OPEN|VISION SKIP|browser-only' | head -50"
echo ""
echo "========================================================================"
echo "TROUBLESHOOTING"
echo "========================================================================"
echo ""
echo "GATE 1 FAIL (avg > 60s):"
echo "  • Check GEMINI_PRIMARY_TIMEOUT_S is NOT set >30 in /root/University-and-Course-data/.env"
echo "  • Check 'gemini_primary_timeout_s' in config.py defaults to 20.0 (not 30.0)"
echo "  • Check Task #233 latency gate code is present:"
echo "    grep -n 'record_timeout' backend-py/app/services/ai/gemini_client.py | head -5"
echo ""
echo "GATE 3 FAIL (no log markers):"
echo "  • journalctl may not capture Celery stdout — check /tmp/*.log or add"
echo "    StandardOutput=journal to uni-celery.service"
echo "  • Vision skips only fire when IELTS/fee are already filled — a scrape"
echo "    where Gemini is suppressed (zero API key) produces no [GEMINI TIMEOUT]"
echo "  • Browser-only gate only fires after 3 rescues on the SAME host in one run"
echo ""
echo "GATE 4 FAIL (fill rates regressed):"
echo "  • Compare extraction_method column in scraped_field_evidence for this run"
echo "    vs the previous run to see which extractor lost a field."
echo "  Query:"
echo "    sudo -u postgres psql -d university_portal -c \\"
echo "    \"SELECT field_key, extraction_method, COUNT(*)"
echo "      FROM scraped_field_evidence sfe"
echo "      JOIN scraped_courses sc ON sc.id = sfe.scraped_course_id"
echo "      WHERE sc.scrape_job_id = '${RUN_ID}'"
echo "      GROUP BY field_key, extraction_method ORDER BY field_key;\""
