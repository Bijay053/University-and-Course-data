#!/usr/bin/env python3
"""
Apply all scraping bug-fix changes to the local working tree (idempotent).

Run from the root of the repo:
    python3 scripts/apply_scrape_bugfixes.py

Then commit + push from prod:
    git add artifacts/university-portal/src/components/scrape-job-card.tsx \
            artifacts/university-portal/src/pages/scraping.tsx \
            backend-py/app/routers/scrape.py
    git commit -m "fix: stop-poll race, force-cancel-all reset, dedup stale-queued"
    git push origin main
"""
import sys, pathlib, re

ROOT = pathlib.Path(__file__).parent.parent

# ── 1. scrape-job-card.tsx ────────────────────────────────────────────────────
CARD = ROOT / "artifacts/university-portal/src/components/scrape-job-card.tsx"
card = CARD.read_text()

# 1a. Add forceResetKey to the props type
OLD_PROPS = "  canRemove?: boolean;\n};"
NEW_PROPS = (
    "  canRemove?: boolean;\n"
    "  /** Incremented by the parent's \"Cancel All\" action to force-reset this card. */\n"
    "  forceResetKey?: number;\n"
    "};"
)
if "forceResetKey?: number" not in card:
    card = card.replace(OLD_PROPS, NEW_PROPS, 1)
    print("scrape-job-card: added forceResetKey prop type")
else:
    print("scrape-job-card: forceResetKey prop type already present, skipping")

# 1b. Destructure forceResetKey in function signature
OLD_SIG = "export function ScrapeJobCard({ slotIndex, universities, onReviewReady, onRemove, canRemove }: ScrapeJobCardProps) {"
NEW_SIG = "export function ScrapeJobCard({ slotIndex, universities, onReviewReady, onRemove, canRemove, forceResetKey }: ScrapeJobCardProps) {"
if OLD_SIG in card:
    card = card.replace(OLD_SIG, NEW_SIG, 1)
    print("scrape-job-card: destructured forceResetKey")
else:
    print("scrape-job-card: signature already updated, skipping")

# 1c. Fix handleStop — add poll cleanup + full state reset
OLD_STOP = (
    "  const handleStop = useCallback(async () => {\n"
    "    if (!activeJobId) return;\n"
    "    setStopping(true);\n"
    "    try { await fetch(`/api/scrape/stop/${activeJobId}`, { method: \"POST\" }); } catch {}\n"
    "    sessionStorage.removeItem(slotKey);\n"
    "    setScraping(false); setStopping(false); setActiveJobId(null); setPhase(\"idle\");\n"
    "  }, [activeJobId, slotKey]);"
)
NEW_STOP = (
    "  const handleStop = useCallback(async () => {\n"
    "    if (!activeJobId) return;\n"
    "    setStopping(true);\n"
    "    // Cancel the poll FIRST so it cannot race and override the idle reset\n"
    "    // with a terminal \"stopped\" status (which would set phase=\"error\").\n"
    "    if (pollRef.current) { clearTimeout(pollRef.current); pollRef.current = null; }\n"
    "    pollInFlightRef.current = false;\n"
    "    pollFailRef.current = 0;\n"
    "    logIndexRef.current = 0;\n"
    "    try { await fetch(`/api/scrape/stop/${activeJobId}`, { method: \"POST\" }); } catch {}\n"
    "    sessionStorage.removeItem(slotKey);\n"
    "    sessionStorage.removeItem(startTimeKey);\n"
    "    setScraping(false);\n"
    "    setStopping(false);\n"
    "    setActiveJobId(null);\n"
    "    setPhase(\"idle\");\n"
    "    setLogs([]);\n"
    "    setProgress(null);\n"
    "    setJobStatus(null);\n"
    "    setUniName(\"\");\n"
    "    setStartTime(null);\n"
    "  }, [activeJobId, slotKey, startTimeKey]);"
)
if "pollRef.current = null; }" not in card:
    if OLD_STOP in card:
        card = card.replace(OLD_STOP, NEW_STOP, 1)
        print("scrape-job-card: fixed handleStop")
    else:
        print("WARNING: handleStop old text not found — check manually", file=sys.stderr)
else:
    print("scrape-job-card: handleStop already fixed, skipping")

# 1d. Add forceResetKey useEffect (after the handleStop block)
FORCE_RESET_EFFECT = (
    "\n  // Force-reset when the parent's \"Cancel All\" fires (forceResetKey increments)\n"
    "  useEffect(() => {\n"
    "    if (!forceResetKey) return;\n"
    "    resetToIdle();\n"
    "  }, [forceResetKey, resetToIdle]);\n"
)
AUTO_FILL_MARKER = "\n  // Auto-fill URL when university is selected"
if "forceResetKey, resetToIdle" not in card:
    if AUTO_FILL_MARKER in card:
        card = card.replace(AUTO_FILL_MARKER, FORCE_RESET_EFFECT + AUTO_FILL_MARKER, 1)
        print("scrape-job-card: added forceResetKey useEffect")
    else:
        print("WARNING: auto-fill marker not found — check manually", file=sys.stderr)
else:
    print("scrape-job-card: forceResetKey useEffect already present, skipping")

CARD.write_text(card)

# ── 2. scraping.tsx ───────────────────────────────────────────────────────────
PAGE = ROOT / "artifacts/university-portal/src/pages/scraping.tsx"
page = PAGE.read_text()

# 2a. Add forceResetKey state
OLD_STATE = "  const [showForceCancelDialog, setShowForceCancelDialog] = useState(false);"
NEW_STATE = (
    "  const [showForceCancelDialog, setShowForceCancelDialog] = useState(false);\n"
    "  const [forceResetKey, setForceResetKey] = useState(0);"
)
if "setForceResetKey" not in page:
    if OLD_STATE in page:
        page = page.replace(OLD_STATE, NEW_STATE, 1)
        print("scraping: added forceResetKey state")
    else:
        print("WARNING: showForceCancelDialog state not found — check manually", file=sys.stderr)
else:
    print("scraping: forceResetKey state already present, skipping")

# 2b. Update executeForceCancelAll to clear slots and increment key
OLD_CLEANUP = (
    "    setScraping(false);\n"
    "    setStopping(false);\n"
    "    setActiveJobId(null);\n"
    "    sessionStorage.removeItem(\"activeScrapeJob\");\n"
    "    setScrapeLogs([]);\n"
    "    setAwaitingApproval(null);\n"
    "  }, [toast]);"
)
NEW_CLEANUP = (
    "    // Reset all ScrapeJobCard slots — clear their sessionStorage keys first\n"
    "    // so resetToIdle() inside each card sees no saved job on its next render.\n"
    "    for (let i = 0; i < 4; i++) {\n"
    "      sessionStorage.removeItem(`scrape_slot_${i}_jobId`);\n"
    "      sessionStorage.removeItem(`scrape_slot_${i}_startTime`);\n"
    "    }\n"
    "    setForceResetKey((k) => k + 1);\n"
    "    // Also reset legacy page-level scrape state\n"
    "    setScraping(false);\n"
    "    setStopping(false);\n"
    "    setActiveJobId(null);\n"
    "    sessionStorage.removeItem(\"activeScrapeJob\");\n"
    "    setScrapeLogs([]);\n"
    "    setAwaitingApproval(null);\n"
    "  }, [toast]);"
)
if "setForceResetKey((k) => k + 1)" not in page:
    if OLD_CLEANUP in page:
        page = page.replace(OLD_CLEANUP, NEW_CLEANUP, 1)
        print("scraping: updated executeForceCancelAll")
    else:
        print("WARNING: executeForceCancelAll old cleanup not found — check manually", file=sys.stderr)
else:
    print("scraping: executeForceCancelAll already updated, skipping")

# 2c. Pass forceResetKey prop to ScrapeJobCard and fix grid
OLD_CARD = (
    "              canRemove={slotIds.length > 1}\n"
    "            />"
)
NEW_CARD = (
    "              canRemove={slotIds.length > 1}\n"
    "              forceResetKey={forceResetKey}\n"
    "            />"
)
if "forceResetKey={forceResetKey}" not in page:
    if OLD_CARD in page:
        page = page.replace(OLD_CARD, NEW_CARD, 1)
        print("scraping: passed forceResetKey to ScrapeJobCard")
    else:
        print("WARNING: ScrapeJobCard canRemove prop not found — check manually", file=sys.stderr)
else:
    print("scraping: forceResetKey prop already passed, skipping")

# 2d. Simplify grid class (remove redundant ternary for 3/4 slots)
OLD_GRID = 'slotIds.length === 2 ? "grid-cols-1 sm:grid-cols-2" : "grid-cols-1 sm:grid-cols-2"'
NEW_GRID = '"grid-cols-1 sm:grid-cols-2"'
if OLD_GRID in page:
    page = page.replace(OLD_GRID, NEW_GRID, 1)
    print("scraping: simplified grid ternary")
else:
    print("scraping: grid ternary already simplified, skipping")

PAGE.write_text(page)

# ── 3. backend-py/app/routers/scrape.py ─────────────────────────────────────
SCRAPE_ROUTER = ROOT / "backend-py/app/routers/scrape.py"
router = SCRAPE_ROUTER.read_text()

# 3a. Add and_ to sqlalchemy imports
OLD_IMPORT = "from sqlalchemy import case, desc, func, or_, select, text"
NEW_IMPORT = "from sqlalchemy import and_, case, desc, func, or_, select, text"
if "and_," not in router:
    if OLD_IMPORT in router:
        router = router.replace(OLD_IMPORT, NEW_IMPORT, 1)
        print("scrape.py: added and_ to sqlalchemy imports")
    else:
        print("WARNING: sqlalchemy import line not found — check manually", file=sys.stderr)
else:
    print("scrape.py: and_ already imported, skipping")

# 3b. Fix dedup check — only block fresh queued jobs (< 2 min)
OLD_DEDUP = (
    "    # Deduplication: if there is already a queued or running job for this\n"
    "    # university, return it instead of creating a duplicate. This prevents the\n"
    "    # \"5 workers claimed the same job\" loop seen when UEL's 403 caused every\n"
    "    # attempt to complete with 0 courses, the UI showed \"completed\", and the\n"
    "    # operator clicked \"Re-run\" multiple times in quick succession.\n"
    "    existing_job = (\n"
    "        await db.execute(\n"
    "            select(ScrapeRuntimeJob)\n"
    "            .where(\n"
    "                ScrapeRuntimeJob.university_id == uni.id,\n"
    "                ScrapeRuntimeJob.status.in_([\"queued\", \"running\"]),\n"
    "            )\n"
    "            .order_by(ScrapeRuntimeJob.created_at.desc())\n"
    "            .limit(1)\n"
    "        )\n"
    "    ).scalar_one_or_none()"
)
NEW_DEDUP = (
    "    # Deduplication: prevent starting a second scrape while one is already\n"
    "    # active for the same university.\n"
    "    #\n"
    "    # Rules:\n"
    "    #   running / awaiting_approval — always block regardless of age.  Two\n"
    "    #     workers scraping the same university at the same time produce\n"
    "    #     duplicate scraped_courses rows and split log streams.\n"
    "    #\n"
    "    #   queued — only block if the job is fresh (< 2 minutes old).  A queued\n"
    "    #     job older than 2 minutes was almost certainly orphaned: either\n"
    "    #     .delay() failed silently (Redis hiccup) or all 4 Celery workers\n"
    "    #     were briefly saturated and the lock expired before a slot freed up.\n"
    "    #     Returning the stale job traps the operator in \"Queued\" forever;\n"
    "    #     allowing a fresh dispatch lets the new task race the orphan —\n"
    "    #     the atomic claim UPDATE (\"WHERE status = 'queued'\") in run_scrape\n"
    "    #     ensures only one of them wins even when both tasks arrive together.\n"
    "    from datetime import datetime as _dt, timezone as _tz, timedelta as _td\n"
    "    _fresh_cutoff = _dt.now(_tz.utc) - _td(minutes=2)\n"
    "    existing_job = (\n"
    "        await db.execute(\n"
    "            select(ScrapeRuntimeJob)\n"
    "            .where(\n"
    "                ScrapeRuntimeJob.university_id == uni.id,\n"
    "                or_(\n"
    "                    ScrapeRuntimeJob.status.in_([\"running\", \"awaiting_approval\"]),\n"
    "                    and_(\n"
    "                        ScrapeRuntimeJob.status == \"queued\",\n"
    "                        ScrapeRuntimeJob.created_at > _fresh_cutoff,\n"
    "                    ),\n"
    "                ),\n"
    "            )\n"
    "            .order_by(ScrapeRuntimeJob.created_at.desc())\n"
    "            .limit(1)\n"
    "        )\n"
    "    ).scalar_one_or_none()"
)
if "_fresh_cutoff" not in router:
    if OLD_DEDUP in router:
        router = router.replace(OLD_DEDUP, NEW_DEDUP, 1)
        print("scrape.py: fixed dedup check (stale queued bypass)")
    else:
        print("WARNING: old dedup block not found in scrape.py — check manually", file=sys.stderr)
else:
    print("scrape.py: dedup fix already present, skipping")

# 3c. Add warning log to silent .delay() failure
OLD_SILENT = (
    "    except Exception:\n"
    "        pass"
)
NEW_SILENT = (
    "    except Exception as _exc:\n"
    "        # Log at WARNING so the failure is visible in worker/API logs on prod.\n"
    "        # The job row stays in 'queued'; requeue_stale will retry after ~2 min.\n"
    "        import logging as _log_mod\n"
    "        _log_mod.getLogger(__name__).warning(\n"
    "            \"start_scrape: broker enqueue failed for job %s (uni %s) — \"\n"
    "            \"job stays queued for requeue_stale recovery: %s\",\n"
    "            job_id, uni.id, _exc,\n"
    "        )"
)
if "broker enqueue failed" not in router:
    # Only replace the first occurrence (the one inside start_scrape)
    idx = router.find(OLD_SILENT)
    if idx != -1:
        router = router[:idx] + NEW_SILENT + router[idx + len(OLD_SILENT):]
        print("scrape.py: added warning log to silent .delay() failure")
    else:
        print("WARNING: silent except block not found in scrape.py — check manually", file=sys.stderr)
else:
    print("scrape.py: broker enqueue warning already present, skipping")

SCRAPE_ROUTER.write_text(router)

print("\nDone. Now run on prod:")
print("  git add backend-py/app/routers/scrape.py \\")
print("          artifacts/university-portal/src/components/scrape-job-card.tsx \\")
print("          artifacts/university-portal/src/pages/scraping.tsx")
print('  git commit -m "fix: stop-poll race, force-cancel-all reset, dedup stale-queued"')
print("  git push origin main")
print("  # Rebuild frontend (if .tsx files changed):")
print("  pnpm --filter @workspace/university-portal run build")
print("  # Restart FastAPI (picks up scrape.py fix):")
print("  pm2 restart university-portal-api  # or however FastAPI is managed on prod")
