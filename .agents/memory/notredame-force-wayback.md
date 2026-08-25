---
name: Notre Dame hybrid blocked-site transport
description: Durable rules for mixing complete CDX coverage, exact archive replay, live rendering, and bounded recovery.
---

## Rule

Use CDX-cached Wayback snapshots as the cheap first transport, but treat the
archive as one source rather than the whole catalogue. When the current sitemap
contains URLs absent from a complete CDX result, route those current-only pages
directly to the one live transport that has passed a representative probe. Never
put known permanent archive misses into the sequential recovery queue.

**Why:** The static proxy tier can spend about a minute returning
ROTATION_FAILED before rendered fetching succeeds. Wayback is much faster for
captured pages, but the current catalogue has many valid pages with no capture.
Archive-only mode therefore finishes quickly with incomplete data, while the
old static-then-render chain is complete but takes hours.

**How to apply:** Enable Wayback-first only after CDX discovery preloads
timestamps. Configure a direct rendered miss fallback only after a live probe
succeeds. Keep primary concurrency conservative and cap recovery by both item
count and wall-clock time.

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
