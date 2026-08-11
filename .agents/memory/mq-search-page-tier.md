---
name: MQ search-page discovery tier
description: Tier 1.5 for MQ: stealth-browser paginates /search page to find research degrees + correct admissions URLs; currently returns 0 (XHR rendering issue).
---

# MQ Search-Page Discovery Tier (Tier 1.5)

## The Problem It Solves
The coursehandbook resolver constructs `/study/find-a-course/courses/<slug>` URLs for
all courses. But many courses live at `/undergraduate/<slug>`, `/postgraduate/<slug>`,
or `/research/<slug>`. In the Aug 2026 production run, 79/198 (40%) courses got
`fetch_failed` because the constructed URL 404s on www.mq.edu.au.

The search page at `https://www.mq.edu.au/search?query=&category=courses` indexes
all 367 international courses with their real admissions URLs — it was added as
Tier 1.5 to supplement the handbook resolver.

## Current Status: Returns 0 Results
Every run so far: Tier 1.5 runs, search page 1 → +0 new courses, exits immediately.

### Root Cause (Hypothesis)
Funnelback/Squiz Matrix search results are **XHR-rendered after `domcontentloaded`**.
The stealth browser (patchright + Xvfb) loads the CF-protected page fine, but:
- `wait_for_selector("a[href*='/study/find-a-course/']", timeout=14_000)` times out
- 3s sleep is insufficient for XHR to complete
- Result: DOM has shell HTML + navigation only, no course result links

### Fix Applied (Aug 2026)
- Changed selector wait to 20s timeout (was 14s)
- Increased settle sleep to 6s on page 1 (was 3s)
- Added `_ALL_ANCHORS_JS` diagnostic: when 0 course links found, emits total anchor
  count + sample hrefs (including `/study/` hrefs) so next run shows what's in DOM
- Added `selector_found=True/False` in the emit message so we know if wait succeeded

### What to Look For Next Run
In the job log, look for:
```
[DISCOVER] MQ: search page 1 (start_rank=1) → +0 new (total 0) [selector_found=False]
[DISCOVER] MQ: search page 1 diagnostic — total_anchors=N, study_hrefs=[...]
```
- If `selector_found=True` → selector worked, check if `_accept_search_url` is
  filtering incorrectly (maybe URL format changed)
- If `selector_found=False` and total_anchors is low (< 30) → CF challenge shell
- If `selector_found=False` and total_anchors > 50 → need even longer wait / networkidle

## Resolver Failure Diagnostics (183/381 failures)
Also added `_fail_reasons` tracking in `_resolve_to_study_urls()`. Now emits:
```
[DISCOVER] MQ: resolver skipped N URL(s) — reasons: generic_title×X; http_404×Y; ...
```
Reason keys: `generic_title` (SSR fallback), `http_404` (discontinued), `network_error`,
`no_title`, `bad_slug`.

**Why:** Without knowing the breakdown, we couldn't tell if failures were structural
(bad titles → always will fail) vs fixable (timeout errors → retry helps).

## Consistent Staging Numbers
Across all Aug 2026 runs: `imported=113, skipped=6` regardless of `total_found` (198–223).
This confirms the 113 are the courses that HAVE valid `/courses/<slug>` admissions URLs.
The remaining 79–110 are courses at other path prefixes that fetch_fail every time.

## How to Apply
When debugging MQ course count regression, check:
1. `total_found` in job log — should be ≥367 if search page works
2. The `search page N diagnostic` emit to understand what DOM contains
3. The `resolver skipped N URL(s) — reasons:` emit to classify failure modes
