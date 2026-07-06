---
name: skip_browser_rescue + scrape_do_skip_fallbacks fetch chain
description: universities that skip both browser rescue and normal httpx/cffi fallbacks lose courses to transient Scrape.do proxy blips; how the retry + Wayback last-resort tiers were added
---

When a university's YAML combines `scrape_do_render: true` +
`scrape_do_skip_fallbacks: true` + `skip_browser_rescue: true` (the standard
config for datacenter-IP-blocked hosts, e.g. Cardiff, QMUL), the
`scrape_do_skip_fallbacks` fast-path in `fetch_html()` becomes the *only*
fetch attempt for every course page — httpx, cffi, and Playwright are all
deliberately skipped because they're blocked too.

**Evolution of the fix (all three landed in `http_fetcher.py`'s `scrape_do_skip_fallbacks`
branch / `fetch_html_wayback()`):**
1. Originally this fast-path had zero retry. A single transient Scrape.do proxy
   blip (502 / "ROTATION_FAILED", common under concurrent load — same signature
   as the Ulster sitemap issue) on both render AND static permanently lost that
   course. QMUL job_5f5ab180197a lost 47/409 courses (~11%) to this gap.
   Fix: retry render once after a 3s backoff if render+static both fail.
2. The retry cut losses to ~5% (7/125, job_aba92c0d3316) but didn't eliminate
   them, because all 3 attempts still go through the *same* Scrape.do proxy
   pool — a pool-wide blip can fail all 3 together. Fix: added a final
   Wayback Machine (`fetch_html_wayback`) attempt after the retry also fails,
   before giving up. Archive.org is not behind the university's live WAF at
   all, so it's a genuinely independent last-resort tier. Only fires after 3
   Scrape.do attempts already failed, so the extra round-trip cost is
   negligible.

3. Even with a Wayback last-resort tier, a residual tail can persist because
   `fetch_html_wayback()` originally used the Availability API
   (`archive.org/wayback/available?timestamp=...`), which returns whatever
   snapshot is *closest in time* to the hint — regardless of HTTP status. A
   403/error snapshot can be "closest" while a perfectly good 200 snapshot
   exists at a different timestamp for the same URL, and the Availability API
   has no way to skip it. Fix: replaced it with a direct CDX search
   (`cdx/search/cdx?...&filter=statuscode:200`, no `closest`/`sort` params —
   those params were empirically unreliable and sometimes returned empty),
   then manually sort all returned 200-status rows by timestamp and fetch the
   most recent via the `id_` raw-HTML modifier. Only query CDX for real gaps —
   some URLs (e.g. QMUL's "MA War Studies") genuinely have zero 200-status
   snapshots ever, which is a true data gap, not a fixable code path.

**Diagnostic gotcha:** `log.info`/`log.warning` calls inside `fetch_html()`
(the Python `logging` module) do NOT appear in the emit()-based job status
log that gets pasted into chat — only `[STAGE]`/`[BROWSER↑ SKIPPED]`/etc
emit() lines do. Grepping a pasted job log for scrape.do branch names will
show zero hits even when that code path fired; you need the raw Celery
worker log (`/tmp/logs/backend-py_Celery_worker_*.log`, which rotates) to see
what actually ran.

**Celery does not hot-reload — a real gap this bug already exposed once.**
The FastAPI workflow runs with `--reload`, so editing files under
`backend-py/app/services/scraper/` takes effect immediately for API routes.
The Celery worker workflow has no `--reload` flag and forks its pool at
startup, so it keeps running the pre-edit code until the "backend-py: Celery
worker" workflow is explicitly restarted — one scrape run after this exact
fix landed still showed the old zero-retry behavior (0 occurrences of the new
log line) purely because the worker process predated the file edit. Always
restart that workflow (not just verify the file diff) after any change under
`app/services/scraper/`, and confirm via `ps -o lstart` vs the file's mtime,
or by grepping the fresh worker log for a marker unique to the new code.

## "Only got 152 of 409 courses" total_found mismatch (fixed twice — final fix inverts the first)

**Symptom v1:** a job reports `total_found=409` but `imported+skipped+errors`
only sums to ~354-358 (looks like ~50 courses silently dropped).

**Symptom v2 (the actual live QMUL bug report — "out of 409 courses, got very
few"):** after an interrupted run resumes, the API reports
`totalFound: 152` (the post-resume-filter *remaining* count) even though
350/409 courses are healthily staged — making a fully-healthy 85%-complete
university look like it only has 152 courses total.

**Root cause:** the RESUME CHECKPOINT logic (`_already_staged_urls` in
`orchestrator.py`) filters already-staged courses out of the work list so a
resumed run doesn't re-pay for extraction. An earlier fix (v1, now
superseded) made this checkpoint set `job.total_found = len(links)` /
`summary["discovered"] = len(links)` to the *trimmed* remaining count, so
finalize's `job.total_found = summary["discovered"]` wouldn't clobber it back
to the stale full-discovery count. That "fixed" the v1 accounting mismatch
but created the v2 bug: total_found now permanently shows the post-resume
remainder (152), not the true discovery count (409), for the rest of that
job's life — including in the UI.

**Final fix (inverts v1):** do NOT shrink `job.total_found` /
`summary["discovered"]` at the resume checkpoint at all — leave both at the
true discovery count. Instead, capture the just-skipped-as-already-staged
count in a local (`_resume_already_staged`) and fold it into `job.imported`
at finalize time: `job.imported = summary["staged"] + _resume_already_staged`.
This keeps `total_found` always meaning "true discovery count" (what
operators expect it to mean) while `imported + skipped + errors` still
reconciles against it with nothing "unaccounted for".

**Diagnostic:** before assuming a job "lost" courses, check for a
`resume_checkpoint` event in `scrape_runtime_logs` for that job. Verify
`already_staged + imported(this-run) + skipped + errors == total_found`.
Also sanity-check `total_found` itself looks like the real catalogue size,
not a suspiciously small number that showed up right after a resume event —
that's the signature of the v2 bug on any code predating this fix.

**Generalizes:** any code path that trims a discovered-links list mid-run
(resume checkpoint, future dedup passes, etc.) should fold the trimmed-out
count into a *progress* counter (`imported`/`skipped`) rather than mutating
`total_found`/`summary["discovered"]` downward — those two fields should only
ever reflect the true catalogue size once discovery + URL-filtering settles,
never a resume/retry artifact.

## Real fetch_failed burst root cause (fixed) — missing Scrape.do concurrency cap

**Symptom:** even after the retry + Wayback tiers above, a run still shows a
large genuine `extract_error`/`fetch_failed` burst (e.g. 116/409 = 28%,
job_8221ce960e02), while manually re-fetching any one of the failed URLs in
isolation succeeds immediately (confirmed by hand with the same
`fetch_html_scrape_do` call).

**Root cause:** `fetch_html_scrape_do()` had NO concurrency limit — unlike the
plain-httpx path (`_sem = asyncio.Semaphore(max_http_concurrency)`), every
call went straight to `httpx.AsyncClient.get()` unbounded. With
`_MAX_PARALLEL_FETCH=12` course-fetch tasks running concurrently, and a
university on `scrape_do_skip_fallbacks + skip_browser_rescue` (so *every*
fetch for *every* course goes through Scrape.do), up to 12+ simultaneous
`render=true` requests hit the one shared Scrape.do account at once. The
account's plan-level concurrent-connection cap then rejects the overflow
(502/429). Because all 12 tasks retry on the same fixed backoff (3s sleep),
the retry lands in the same saturated window too, so the retry doesn't help —
only Wayback (a genuinely different origin) rescued some, not all.
`scrape_do_rate_limit_per_sec` (the existing Redis token-bucket) defaults to
0.0 (disabled) and wouldn't have helped anyway — it smooths cross-worker call
*rate*, not simultaneous in-flight *connections* from one process.

**Fix:** added a dedicated `asyncio.Semaphore(settings.max_scrape_do_concurrency)`
(default 5) wrapped directly around the outbound Scrape.do HTTP call in
`fetch_html_scrape_do()`, independent of `_MAX_PARALLEL_FETCH`/`_sem`. Verified
end-to-end: QMUL re-run (job_98f90a8c6023) went from 116 fetch_failed / 407
render calls needed to **0 fetch_failed**, with exactly 407 render + 2 static
calls for 409 courses (one call per course, no wasted retries at all).

**Generalizes beyond QMUL:** any university on `scrape_do_skip_fallbacks`
(Cardiff, UWL, WLV, HUD) shares the same single Scrape.do account and was
equally exposed to this burst-failure mode, even though only QMUL happened to
surface it loudly. No YAML change needed — the semaphore is global.

## Regression despite the semaphore fix — per-process semaphore doesn't bound the fleet

**Symptom:** even after the `max_scrape_do_concurrency` semaphore fix above,
a later QMUL run (job_4fb674e585b2, 2026-07-06) lost 279/409 (~68%) courses
to `fetch_failed` — far worse than the 0% the semaphore fix had achieved.

**Root cause:** `_scrape_do_sem` is a plain in-process `asyncio.Semaphore`.
Celery's default prefork pool spawns 8 *separate OS processes*
(`--concurrency=8`), each importing `http_fetcher.py` fresh and getting its
own semaphore instance. So `max_scrape_do_concurrency=5` only bounds
concurrency *within one worker process* — if other Celery tasks (other
university scrapes) are running in sibling worker processes at the same
time, real account-wide concurrent Scrape.do connections can be up to
`8 * 5 = 40`, still enough to saturate/rate-limit the shared account. The
one genuinely cross-process mechanism, `scrape_do_rate_limit_per_sec` (a
Redis token bucket, see `rate_limiter.py`, built for exactly this problem),
was sitting at its default of `0.0` (disabled) the whole time.

**Fix (two parts, both in this round):**
1. Widened the existing single retry (render only) into a 3-step
   exponential-backoff ladder — render → static → render at 3s/8s/15s —
   before falling through to Wayback, since one retry proved insufficient
   once contention got worse.
2. Changed `settings.scrape_do_rate_limit_per_sec` default from `0.0` to
   `3.0`, actually engaging the pre-built cross-process throttle so bursts
   get smoothed fleet-wide, not just per-process. This is fail-open (30s
   max wait, then proceeds anyway) so it can only add latency, never block.

**Lesson:** when a university's YAML relies on a *single* fetch tier with no
fallback (`scrape_do_skip_fallbacks` + `skip_browser_rescue`), any
in-process-only concurrency guard is a false sense of safety once multiple
Celery prefork workers exist — always check whether a cross-process
Redis-backed mechanism already exists (it did here, unused) before adding
more in-process guards or retries.

## Rate-limiter fix broke a sibling university — discovery starved by another job's burst

**Symptom:** the very next day after the `scrape_do_rate_limit_per_sec=3.0`
fix above shipped, a *different* university (Cardiff, job_68778b8f7bb2,
`discovery.scrape_do_skip_fallbacks: true`) failed discovery with "exceeded
300s deadline" after crawling only 4/25 listing pages, despite Cardiff's own
config being unchanged. `scrape_do_render_calls`/`scrape_do_static_calls`
were both 0 for the job (those job-level counters are keyed off a
ContextVar only set during course-level extraction, so they don't capture
discovery-phase Scrape.do activity — don't use them to rule out Scrape.do
involvement in a discovery-phase stall).

**Root cause:** the new fleet-wide Redis token bucket has ONE small shared
budget (`capacity=ceil(3.0)=3`/sec) across *every* Scrape.do caller,
regardless of which university or which phase (discovery vs. course
extraction) is calling. A concurrent QMUL run (job_7cf9059a5269, started
8s before Cardiff's job, confirmed via `started_at` overlap in
`scrape_runtime_jobs`) was issuing many parallel course-extraction Scrape.do
calls (now itself calling more per course thanks to the 3-step retry ladder
in the same fix), saturating that 3/sec budget. Cardiff's discovery loop is
strictly *sequential* — one listing-page fetch at a time, awaited — so each
call had to wait for a free token, up to `_MAX_WAIT_S=30s` per call. A
handful of such waits blew straight through the 300s discovery deadline.
Discovery is the *victim* of another job's burst here, not the cause of one.

**Fix:** added a `rate_limit: bool = True` parameter to
`fetch_html_scrape_do()`; the discovery-phase fast-path call sites
(`scrape_do_skip_fallbacks` branch in `fetch_html()`, and the
`_apply_render_listing_pages` listing-page fallback in `orchestrator.py`)
now pass `rate_limit=False` so they skip `acquire_scrape_do()` entirely and
never queue behind a sibling job's course-extraction burst. Course-level
extraction calls (the original QMUL burst source, and the reason the
limiter exists) are untouched and still throttled at 3/sec.

**Generalizes:** any time a fleet-wide shared-resource throttle is added to
fix one heavy consumer's burst, check every *other* caller of that same
resource for a fundamentally different usage shape (sequential + low-volume
+ hard-deadlined, vs. parallel + high-volume + retryable) before assuming a
single shared budget is safe for all of them. A budget sized for smoothing
bursty parallel retries can starve a serial caller with a tight deadline
even though its own volume never approaches the limit.

## Second Cardiff regression, no concurrent job this time — one stuck page can eat the whole discovery deadline

**Symptom:** a *third* Cardiff discovery stall (job_82781680a1e4, 2026-07-06),
this time with NO overlapping job running (confirmed via `scrape_runtime_jobs`
`started_at` — ruled out the rate-limiter-starvation cause above). The BFS
only got through 2/25 listing pages before the 300s deadline fired, with
total silence in between (no per-page log lines) — a different failure mode
from the previous regression's many-small-waits pattern.

**Root cause:** `fetch_html_scrape_do()`'s outbound call uses a 90s
`httpx.AsyncClient(timeout=90)`. For `scrape_do_skip_fallbacks=True`
universities, a single `fetch_html()` call can try static-then-render
sequentially inside that one call — up to 180s. `discovery.py`'s BFS loop
then calls `fetch_html()` up to 3 times per candidate page (immediate retry
+ bare-URL-without-query-string retry), each with NO independent bound. One
genuinely slow/hanging page — no error, no exception, just a scrape.do
response that takes a long time — can singlehandedly consume 360-540s worst
case, more than the entire `discovery_phase_timeout_s` (300s) budget, and the
BFS never advances past it.

**Fix:** added `settings.discovery_page_fetch_timeout_s` (default 45s) and
wrapped every discovery-level `fetch_html()` call in `discovery.py`'s BFS
loop with `asyncio.wait_for(..., timeout=...)`. A timeout is treated exactly
like the existing "fetch failed" outcome (log + move to next page/retry tier)
so one bad page degrades gracefully instead of stalling the whole crawl.

**Generalizes:** any code that wraps a long single-fetch operation in an
outer retry loop needs BOTH tiers bounded — the outer loop's own retry count
alone does not cap wall-clock time if the inner call's own timeout is large
relative to the outer deadline. When adding a hard deadline around a
multi-step operation (`asyncio.wait_for` at the top), also check every step
inside it has its own materially-smaller bound, or one step can still eat
the whole budget alone.
