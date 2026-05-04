#!/usr/bin/env python3
"""
Apply the scraping bug-fix changes to the local working tree.

Run from the root of the repo:
    python3 scripts/apply_scrape_bugfixes.py

Then commit + push:
    git add artifacts/university-portal/src/components/scrape-job-card.tsx \
            artifacts/university-portal/src/pages/scraping.tsx
    git commit -m "fix: stop-poll race and force-cancel-all card reset"
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

print("\nDone. Now run:")
print("  git add artifacts/university-portal/src/components/scrape-job-card.tsx \\")
print("          artifacts/university-portal/src/pages/scraping.tsx")
print('  git commit -m "fix: stop-poll race and force-cancel-all card reset"')
print("  git push origin main")
