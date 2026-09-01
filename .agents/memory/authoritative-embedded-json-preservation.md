---
name: Authoritative embedded JSON preservation
description: Why site-specific structured-data overrides must receive the original response rather than post-processed HTML.
---

When a university's authoritative course fields live in embedded JSON, preserve the original HTTP response before generic compaction, audience slicing, or text-oriented transformations and pass that preserved document to the late site-specific override.

**Why:** A page can remain visually and textually usable after processing while its script blocks are gone. The ordinary extractors then return plausible aggregate values, and the late authority silently has nothing to override. This previously caused aggregate search-card dates and provider-suffixed titles to reach staging even though the raw page contained correct course-detail blocks.

**How to apply:** Capture raw HTML immediately after fetch and before any mutation. Keep the site-specific parser deterministic and independent of ambient context where the provider identity is intrinsic to the hostname. When pages repeat aggregate and detail blocks, select the last usable course-detail block consistently for dates and locations.