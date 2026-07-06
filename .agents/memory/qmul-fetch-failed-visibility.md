---
name: QMUL fetch_failed visibility gap
description: Why "no traces" course losses happened despite Errors:0, and where the real fix already lives
---

The orchestrator tracks `summary["fetch_failed"]` (courses whose fetch came back empty and had no browser-rescue fallback, e.g. `skip_browser_rescue: true` universities) separately from `summary["errors"]`. For a long time this counter was invisible in the live `"══ DONE ══"` status line shown to operators — only Found/Staged/Skipped/Errors were printed, so a run could show `Errors:0` while hundreds of courses vanished with zero visible trace.

**Where it's actually fixed today:**
- `run_summary.py` / `ScrapeRunSummary` model (`scrape_run_summary` table, `fetch_errors` + `skipped_fetch_failed` columns) already persists this correctly per run — this is the source of truth for dashboards/alerts. Don't re-add a duplicate `fetch_failed` column on `scrape_runtime_jobs`; it's redundant.
- The retry ladder for the actual fetch failures (render→static→3-step backoff→Wayback) lives in `http_fetcher.py`'s `fetch_html()`, not in `single_course.py`'s browser-rescue-skip branch. If `skip_browser_rescue`/`skip_per_course_browser` is set, `single_course.py` has already exhausted `fetch_html()`'s full fallback chain (including Wayback) before it ever gets to the `fetch_failed` return — adding another retry loop there is pure duplication.
- The one remaining gap (fixed 2026-07-06): the `"══ DONE ══"` live status line itself didn't print `fetch_failed`. Now includes `FetchFailed:{n}` explicitly.

**Why:** two false starts happened before finding this — first assumed no retry existed at all (wrong, `http_fetcher.py` already has it, confirmed by `test_qmul_scrape_do_retry.py`), then assumed no DB persistence existed (wrong, `ScrapeRunSummary` already has it, confirmed by `test_run_summary.py`).

**How to apply:** before adding new retry/counter/column plumbing for scraper fetch failures, grep for `fetch_failed` across `run_summary.py`, `http_fetcher.py`, and their test files first — the fix may already exist from an earlier session.
