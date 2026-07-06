---
name: Cloudflare 429 vs block classification in fetch_html ladder
description: Why treating rate-limit 429s the same as hard CF challenges (403/503) burns discovery-phase time budgets, and how per-uni Wayback opt-out was silently ignored.
---

`fetch_html()`'s Cloudflare-block classifier historically treated any 403/429/503
carrying `cf-ray`/cloudflare headers identically, escalating straight through
the full ladder (cffi retry → Wayback → Scrape.do static → Scrape.do render).

A 429 is Cloudflare's rate limiter, not a bot-challenge page. For hosts where
the origin normally serves fine over cffi/httpx (confirmed working transport),
escalating to a different transport (Wayback/Scrape.do) does nothing to fix a
rate limit — only waiting does. Paying that ladder's full round-trip latency on
every rate-limited page, multiplied across a large BFS page budget, is what
exhausted a 300s discovery-phase deadline for Kingston (bfs_page_budget=35,
429s starting ~page 11).

**Why:** discovery_phase_timeout_s wraps the whole BFS crawl in one deadline;
per-page latency compounds directly into that budget with no individual page
having an obviously "wrong" duration to blame.

**How to apply:** when diagnosing a new "[DISCOVER] Discovery phase exceeded
Ns deadline" failure, check whether the university's actual failure mode is a
429 (rate limit, fixable by backoff on the same transport) vs 403/503 (real
challenge, needs a transport/proxy change) before reaching for Scrape.do flags.

Separately: the per-university `discovery.use_wayback: false` config flag was
only wired into the orchestrator's discovery-wide Wayback CDX sweep — the
per-request Wayback tier inside `fetch_html()`'s CF-block ladder ignored it
entirely. Any uni that explicitly opts out of Wayback (documented as
"archive.org has nothing useful here") was still paying an archive.org
round-trip on every blocked page. Check both wiring points when a per-uni
config flag doesn't seem to be taking effect — flags are sometimes read at
only one of several call sites.
