---
name: UniSQ browser timeout + stale-code NameError pattern
description: UniSQ Next.js SPA times out in Playwright; skip_browser_rescue:true fixes it. Stale Celery worker is first suspect for NameError on recently-added local variables.
---

## Rule
When a NameError fires for a variable that IS correctly defined in the on-disk code, suspect a **stale Celery worker** before debugging the code itself. Celery forks workers at startup; any code change after the fork is invisible until the worker is restarted.

**Why:** The `_online_filter_enabled` variable was added inside a `try/except` block in `guards.py`. The worker that was already running had the old bytecode (without the block) but the references at lines 634/794 remained, causing NameError on every `should_stage_course` call. The on-disk code was correct; a worker restart fixed it.

**How to apply:** If a systematic extraction error (100% of courses fail with the same NameError) appears right after a code change, restart the Celery worker immediately before doing any other debugging.

## UniSQ-specific: browser timeout

UniSQ (`www.unisq.edu.au`) is a Next.js SPA. Playwright times out on **every** course page after 25 s when the `?studentType=international` query param is appended, because the JS bundle never reaches `networkidle`. The plain-HTTP `httpx` fetch gets a fully SSR'd response (200 OK, ~200 KB) that already contains course name, degree level, IELTS, duration, and study mode.

**Fix in YAML (unisq.yaml):**
```yaml
extraction:
  skip_browser_rescue: true   # SSR HTML is sufficient; browser times out on every page
```

This gates both browser paths in `single_course.py`:
- HTTP-failure browser fallback (~line 1515): `_skip_all_browser = _skip_rescue or _skip_per_course`
- Sparse-static rescue after Gemini-primary (~line 3726): `if not _skip_rescue and fee+duration both blank`

Fees live on the central fee page (`/study/fees-and-scholarships`), not per-course — so skipping browser costs nothing. The companion YAML flags `stage_on_parser_error: true` and `require_international_fee: false` handle the missing-fee case.

## UniSQ-specific: location drift

Treat the primary quick-facts Location list as authoritative, preserving every
physical campus while removing `Online` and `External` only from the physical
location output. Before changing that parser for a reported mismatch, compare
the selected field evidence snippet with a fresh fetch of the same URL.

**Why:** UniSQ changed several published Location lists between two same-day
checks. The saved evidence proved the scraper had faithfully captured the older
lists; a fresh scrape correctly captured the newer values. Without checking
provenance, ordinary source drift looked like a systemic parser defect.

**How to apply:** Audit all staged UniSQ rows against fresh primary quick-facts,
rerun with forced discovery when the source changed, and reserve parser changes
for cases where saved raw evidence contained the correct list but normalization
or selection produced the wrong value.
