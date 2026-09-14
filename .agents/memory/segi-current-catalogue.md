---
name: SEGi dual current catalogue transport
description: Authoritative sources and transport boundaries for SEGi University and Colleges scraping.
---

Treat `university.segi.edu.my` and root-level programme pages on `www.segi.edu.my` as distinct current catalogues under SEGi University & Colleges. Keep the university catalogue primary and supplement it with qualification-bearing WordPress pages indexed by the `Programme ID` marker.

**Why:** The university host has the university catalogue and an incomplete TLS chain, while the Cloudflare-protected main host publishes a separate, current, campus-specific college catalogue. Broad WordPress search also returns marketing, policy, and faculty pages, and generic render retries can multiply paid calls.

Scrape.do can return persistent `ROTATION_FAILED` responses for the Colleges host across standard, super, geo-pinned, and asynchronous pools. This is an upstream origin-routing outage, not evidence that discovery is incomplete.

**How to apply:** Keep the university host on exact-host, HTTPS-only direct TLS handling with redirect revalidation. Discover the main-host supplement through bounded rendered WordPress search, require exact host/root path plus programme and qualification evidence, and bound every rendered attempt below the whole-course deadline so provider failures cannot stale the worker heartbeat. Retry only within that deadline and require a successful single-page provider probe before launching a full production proof. Read campus only from course-owned metadata/hero content; never use a global SEGi campus default. Treat explicit “Online Mode” titles as authoritative Online delivery. Leave unpublished tuition blank rather than inferring ancillary charges.