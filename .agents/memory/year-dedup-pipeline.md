---
name: Year-based URL deduplication pipeline
description: Pre-extraction dedup for year-versioned course URLs (e.g. SCU /2026/ vs /2027/); order and fields that are live.
---

Phase A.5c in `orchestrator.py` runs on the `links` list BEFORE `extract_course()` is dispatched.

**Order:**
1. `ignore_urls_matching` (substring drop) — Step 1
2. `course_year.ignore_years` (year-in-path drop) — Step 2
3. `slug_without_year` dedup — Step 3
   - If `prefer_urls_matching` set: first candidate whose URL matches a pattern wins (tiebreaker)
   - Falls back to year-mode: `keep_preferred_year` (uses `preferred_year`), `keep_latest`, `keep_current`

**Why pre-extraction matters:** 2027 URLs never reach Gemini → no wrong fees enter staging → no duplicates to clean up later.

**Correct SCU recipe (Recipe Editor → Year & Duplicates):**
- Mode: keep_preferred_year
- Preferred Year: 2026
- Ignore Years: [2027]
- Duplicate Key: slug_without_year
- Ignore URLs Matching: ["/2027/"]
- Prefer URLs Matching: ["/2026/"]

**Result:** 198 raw links → ~99 extractable (one year variant per course).
