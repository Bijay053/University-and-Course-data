---
name: Large-scrape resume checkpoint + fleet contention limits
description: How 500+ course scrapes survive the Celery time ceiling and avoid 429 storms (resume + contention design decisions).
---

# Large-scrape resume + contention bounding

## The decision that matters: raised ceiling + resume, NOT chunk handoff
The original spec offered two ways to "decouple job lifetime" from the 45-min
Celery ceiling: (a) an in-process chunk-handoff state machine, or (b) raise the
ceiling and make re-runs resume cheaply. **Chosen (b).**

**Why:** chunk handoff mutates the requeue_count / uni-lock / atomic-claim
machinery in scrape_tasks.py + orchestrator.py — code that cannot be live-verified
in the dev environment (no prod keys/network). A subtle bug there breaks EVERY
university. Raised ceiling + resume is robust and testable: even a catalogue that
still exceeds the ceiling completes across re-runs without losing progress.

**How to apply:** if asked to "make chunks hand off" or "split the job", prefer
extending the resume checkpoint over rebuilding the requeue machinery, unless the
work can be verified end-to-end against the live site.

## Resume checkpoint mechanics
- `scrape_resume_enabled` (default True) gates everything.
- `_clear_stale_dedup` must NEVER wipe (1) the current job's own staged rows, nor
  (2) recently-interrupted resumable jobs (status `queued`/`failed` within
  `scrape_resume_window_minutes`). It still only ever deletes `status='pending'`
  rows — rejected rows are reviewer decisions and are untouchable.
- Before the extraction batch loop, `run_scrape` filters out links whose
  normalised URL is already staged (non-rejected) for the university. Rejected
  rows are deliberately re-attempted (stage_course owns the rejection-block).
- URL match uses `_normalize_course_url`: strip scheme + leading `www.` + trailing
  slash, but **preserve the query string** — some unis key the international-fee
  view off `?international=true` and those are genuinely distinct pages.

## Contention bounding (all default-OFF, all fail-open)
- `rate_limiter.py` — Redis fixed-window token bucket. `acquire(resource, rate)`
  returns True immediately when rate<=0 (disabled) or on any Redis error
  (fail-open: a Redis outage must never block scraping). Wired into
  `fetch_html_scrape_do` (scrape_do) and both `gemini_client` call sites (gemini).
- `max_concurrent_scrapes` — fleet-wide cap via a Redis ZSET `scrape:active_runs`
  (member=runtime_job_id, score=start ts). Stale entries older than the hard
  ceiling are swept on each acquire so a crashed worker never permanently leaks a
  slot. Over-cap jobs are aborted with reason `max_concurrent_scrapes` (safe now
  that re-runs resume) rather than risking the requeue machinery.

**Why default-off:** the 8 prefork workers share ONE Scrape.do account + ONE
Gemini quota; in-process semaphores multiply by worker count and can't bound real
contention. These knobs let an operator throttle the fleet without a deploy, but
shipping them off keeps behaviour byte-identical until opted in.

## Celery ceiling
`celery_app.py` reads `task_soft_time_limit`/`task_time_limit` from
`settings.scrape_task_soft/hard_time_limit_s` (defaults 7200/7500) instead of the
old hardcoded 2700/3000. Hard must stay > soft (SIGKILL only after an unhandled
SoftTimeLimitExceeded).

## Pre-existing test failures (not ours)
`tests/test_location.py::test_uwl_yaml_*` expect `central_page_ug`/`_pg` in
`scraper_config/unis/law_1902.yaml`, which the YAML lacks. Unrelated to this work.
