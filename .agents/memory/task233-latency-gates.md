---
name: Per-course scrape latency gates (Gemini timeout / vision / browser-only)
description: Design rationale for the three latency-cut gates in the per-course scrape path.
---
Three independent gates cut wasted per-course time on large scrapes without
regressing field-fill:

1. **Gemini per-call timeout lives INSIDE `gemini_client.generate()` at the SDK
   boundary** (not wrapping the caller). `asyncio.wait_for(timeout_s)` with
   `except asyncio.TimeoutError` placed BEFORE the generic `except Exception`;
   on timeout it does NOT retry and returns a skipped response. Timeouts are
   recorded via `record_timeout()` into the SAME `_recent_failures` deque /
   threshold as quota errors, so slow calls + 429s JOINTLY trip the circuit
   breaker. Default knob `gemini_primary_timeout_s=20.0` (was a hard 30s dead
   wait per course). `gemini_primary.extract_primary` must pass `timeout_s` and
   must NOT keep its own outer wait_for.
   **Why:** a guaranteed-empty 30s Gemini wait happened dozens of times per
   batch on Ulster; bounding it at the SDK boundary is the only place that also
   feeds the breaker.

2. **Vision OCR early-exit**: `maybe_vision_refetch` returns `({}, [])` when
   there is no tier-0 (English-section) image AND every `_ENGLISH_OVERALL_SLOTS`
   is already filled. Tier-0 present, or any overall slot missing, still runs
   vision so recoverable sub-bands are not skipped.

3. **Confirmed-browser-only host gate**: a run-scoped `ContextVar` tally in
   `per_course_browser.py` counts genuine browser rescues per host; after 3,
   `is_confirmed_browser_only()` skips the always-failing initial HTTP fetch for
   the rest of the run. Reset at `orchestrator.run_scrape()` and
   `repair.run_repair()`. A rescue is counted only when HTTP was actually
   attempted AND the browser returned substantive HTML
   (`len >= _BROWSER_RESCUE_MIN_HTML_LEN = 2000`).
   **Why:** without the length floor, 3 Cloudflare "Just a moment" challenge
   shells (truthy but tiny) could wrongly route every later course straight to
   browser and skip HTTP that actually serves them.

ContextVar tally is safe under per-course concurrency: a mutable dict is stored
in the ContextVar before `gather()`, child asyncio tasks inherit the same dict
reference, and increments are synchronous in one event loop.
