---
name: Discovery URL cache (C1) design lessons
description: Discovery link cache — scope identity, gates, side-channel outputs, and provider-payload exclusions
---

# Discovery URL cache lessons

**Rule:** A cached fast-path that skips a pipeline phase must also carry that
phase's *side-channel outputs*, not just its primary result.

**Why:** The 7-day discovery URL cache pre-seeds `links` and skips BFS — but
BFS also populated `_discover_blocked_fee_urls` (fee pages rejected as course
candidates), which the central-fee-page fallback needs later. First version
silently degraded fee extraction on cache-hit runs. Fix: fee URLs are stored in
the same JSONB list marked `fee_page: true` and split back out on read.

**How to apply:** Before caching any phase's output, grep for every mutable
sink/out-param the skipped code writes to (`_sink`, `.extend(`, `.append(`)
and either persist those too or document why they're safe to lose.

**Rule:** Discovery links are reusable only when both the normalized start URL
and the effective discovery configuration match the run that produced them.
Treat legacy cache entries without scope identity as misses.

**Why:** A narrow category crawl can produce a healthy-looking partial result
with no fetch failures. When the cache was keyed only by university, Deakin's
18 Education URLs were later reused for a root-domain scrape that should have
covered every configured discipline seed.

**How to apply:** Bind each entry to a stable fingerprint of the start URL,
discovery config, and recipe inputs. A scope mismatch must fail open to full
discovery and then replace the old entry; repeated identical runs may still hit.

Other gates that must stay in sync:
- Read and write must use the same configured course-coverage floor, excluding
  fee-page metadata, so a plausible-looking partial discovery is never reused
  or allowed to replace a healthy result.
- Forced discovery bypasses reads; stale, mismatched, degraded, unscoped, and
  provider-payload results remain ineligible for reuse.
- Cache hit must also disable browser discovery (`_always_browser=False`) and
  the Wayback supplement — everything else is `if not links`-gated naturally.
