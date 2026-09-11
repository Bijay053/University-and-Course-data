---
name: SEGi dual current catalogue transport
description: Authoritative sources and transport boundaries for SEGi University and Colleges scraping.
---

Treat `university.segi.edu.my` and root-level programme pages on `www.segi.edu.my` as distinct current catalogues under SEGi University & Colleges. Keep the university catalogue primary and supplement it with qualification-bearing WordPress pages indexed by the `Programme ID` marker.

**Why:** The university host has the university catalogue and an incomplete TLS chain, while the Cloudflare-protected main host publishes a separate, current, campus-specific college catalogue. Broad WordPress search also returns marketing, policy, and faculty pages, and generic render retries can multiply paid calls.

**How to apply:** Keep the university host on exact-host, HTTPS-only direct TLS handling with redirect revalidation. Discover the main-host supplement through bounded rendered WordPress search, require exact host/root path plus programme and qualification evidence, and use one rendered attempt per course. Read campus only from course-owned metadata/hero content; never use a global SEGi campus default. Treat explicit “Online Mode” titles as authoritative Online delivery. Leave unpublished tuition blank rather than inferring ancillary charges.