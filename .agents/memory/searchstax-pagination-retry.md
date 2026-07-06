---
name: SearchStax pagination silent truncation
description: A single transient page-fetch failure mid-pagination used to silently truncate SearchStax results with no visible error — looked like a mysterious course-count drop.
---

Both `_fetch_links_only` (links_only mode) and the full/HUD-style loop in
`fetch_searchstax_links` paginate a Solr core with a `while True` loop. The
original code treated ANY exception on a single page fetch as "endpoint done"
— it broke the loop and returned whatever had accumulated so far, with only a
`log.error` (not surfaced via `emit`, so operators never saw it in the job
log/UI).

**Why this matters:** the summary line (`SearchStax URLs found: N | ...`)
computes exclusion counters (title prefix/substring) that are all correctly
zero in this failure mode, so a partial result looks identical to "N found,
M queued, rest legitimately filtered" — there is no signal telling you a page
actually errored out. Confirmed case: QMUL reported 409 docs found but only
300 queued; the 4th Solr page failed transiently (likely a public-token
rate limit from 3 rapid back-to-back requests) and pagination gave up
silently.

**How to apply:** any Solr/paginated-API polling loop like this should (1)
retry a failed page a few times with backoff before giving up, (2) add a
small pacing delay between pages to avoid rate-limiting shared/public API
tokens, and (3) if it still can't recover, emit a real warning (via `emit`,
not just `log.error`) naming the `start` offset and how many results were
actually obtained vs. how many were expected — so a genuine failure is
visibly different from legitimate filtering.
