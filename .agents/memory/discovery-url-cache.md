---
name: Discovery URL cache (C1) design lessons
description: 7-day per-university discovery link cache — gates, side-channel outputs, and provider-payload exclusions
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

Other gates that must stay in sync:
- Read: age < 7d, ≥5 *course* links (fee entries excluded from count),
  bypassed by `forceDiscovery` request flag and for SearchStax-provider unis.
- Write: only healthy runs (≥5 course links AND fetch-fail rate < 30%) may
  overwrite; provider-payload links (`searchstax_result`/`swiftype_result`/
  `payload` keys) are never cached — they'd be served stale and are huge.
- Cache hit must also disable browser discovery (`_always_browser=False`) and
  the Wayback supplement — everything else is `if not links`-gated naturally.
