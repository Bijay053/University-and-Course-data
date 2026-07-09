---
name: Fetch Reliability Overhaul T01-T08
description: ScrapedoAccountError + retry in fetch_html_scrape_do; sweep pass + failure-rate guard in orchestrator; new job statuses.
---

## Rule
`fetch_html_scrape_do` now retries 429/5xx up to 3 times (backoffs 2s/8s/30s).
401/403 raises `ScrapedoAccountError` immediately (no retry).
404/410 returns None immediately (page-not-found, no retry).
5xx with `ROTATION_FAILED` in body AND render=False returns None immediately
(no retry — proxy connect refusal never heals by retrying static; lets the
caller's render tier fire while budget remains). render=True keeps the ladder.
See jcu-cloudflare-lesson.md.

## Why
JCU run job_830c773066e0 saved only 7/103 courses — 96 returned None because
Scrape.do rate-limit rejections had zero retry path.

## How to apply
- `ScrapedoAccountError` is defined in `http_fetcher.py` (top of file after `_sem`).
  Propagates through `_extract_only` → `_bounded` → `asyncio.gather` results.
- `_scrape_do_sem` (Semaphore) is released between retry attempts; re-acquired
  before each attempt — sibling coroutines can proceed during the sleep.
- Retry-After header is honoured: `max(default_backoff, float(retry_after))`.

## Orchestrator additions (same session)
- `_sweep_links: list[dict]` collects every fetch_failed link during batch staging.
- After all batches: sequential sweep (2s gap between calls, concurrency=1).
  Recovered courses: `summary["staged"] += 1`, `fetch_failed -= 1`.
- T05 failure-rate guard (runs AFTER sweep):
  - `_fetch_fail_n / max(1, _discovered_n) > 0.30` → `job.status = "failed_degraded"`
  - `> 0.10` → `job.status = "completed_with_warnings"`
- New terminal statuses: `failed_degraded`, `failed_provider`, `completed_with_warnings`.
  - Terminal-status guard at finalize updated to include all three.
  - Resume-window claim query: status IN (..., 'failed_degraded', 'failed_provider').
  - Active-job resume query: status IN (..., 'failed_degraded', 'failed_provider', 'completed_with_warnings').

## Tests
`backend-py/tests/test_fetch_reliability.py` — 19 tests, all pass.
Classes: TestFetchHtmlScrapeDoRetry, TestScrapedoAccountError,
TestRetryAfterHeader, TestFailureRateGuardThresholds.
