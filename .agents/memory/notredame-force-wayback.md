---
name: Notre Dame hybrid blocked-site transport
description: Durable rules for mixing complete CDX coverage, exact archive replay, live rendering, and bounded recovery.
---

## Rule

Use the fastest representative-probe winner as the primary transport. As of
September 2026, Scrape.do rendered live pages complete materially faster than
Archive.org, so rendered live is primary and CDX-cached Wayback is the last
resort. Treat the archive as one source rather than the whole catalogue, and
never put known permanent archive misses into the sequential recovery queue.

**Why:** Transport performance changed over time. Archive snapshots varied from
roughly 9 to 35 seconds per page while rendered live probes returned equivalent
HTML in roughly 5 to 19 seconds. Direct/static requests remain blocked. The old
Wayback-first decision was correct when cached snapshots took about 1.4 seconds,
but became the dominant latency once Archive.org slowed.

**How to apply:** Re-probe several representative pages before changing order.
Skip direct/static tiers, keep primary concurrency conservative, retain cached
Wayback as last resort, and cap recovery by both item count and wall-clock time.

## Sustained rendered-provider congestion

Cap each rendered live attempt at 20 seconds and move immediately to Wayback
after the first failure. Do not use the generic static-plus-multi-retry ladder
for this host. If Wayback has no snapshot, make one final bounded render attempt.

**Why:** Short probes can succeed in 5–19 seconds while sustained production
loads later produce synchronized provider hangs. Without the inner cap, four
requests consume the shared 90-second course deadline together, so the archive
fallback never runs and timeout errors accumulate in waves.

**How to apply:** Judge the policy from a full production batch, not a few fast
probes. Preserve the outer course deadline, but reserve enough of it for the
independent archive transport and downstream extraction.

## Archive identity and completeness

Course deduplication identity and archive-scope identity are different:

- Course identity may collapse transport noise such as HTTP/HTTPS, `www`, and
  known tracking parameters.
- Archive completeness must preserve the exact queried host and optional port.
  A complete wildcard query for `www` does not prove that an apex-host URL is
  absent.
- Only a successful, non-truncated wildcard CDX response is authoritative.
- Cached replay must retain the exact CDX original URL, including HTTP scheme
  and legacy port; replaying a timestamp against a rewritten HTTPS URL can fail.

**Why:** Reusing the aggressive course-dedup key for archive authority can create
false permanent misses across host aliases. Rewriting the original replay URL
can likewise turn a valid historical capture into a false fetch failure.

**How to apply:** Use the broad identity only for discovery/resume/recovery
deduplication. Use a separate host-preserving key for authoritative CDX scopes,
and cache each timestamp with its exact captured original URL.
