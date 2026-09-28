---
name: UWL rendered listing coverage
description: Intermittent provider failures can omit nonconsecutive listing pages despite successful static-to-rendered failover.
---

Rendered access working is not, by itself, proof that a paginated catalogue is complete. A provider can return HTTP 502 for nonconsecutive rendered listing pages while successfully rendering the pages around them. A configured full-catalogue listing needs every page to succeed after its own bounded retries; overall URL count cannot substitute for per-page coverage.

**Why:** A live University of West London attempt switched successfully from static to rendered access but missed two nonconsecutive pages and discovered fewer URLs than its configured floor. A fresh attempt loaded all pages and cleared the floor. A single count threshold might miss this class of failure if enough unrelated URLs inflate the total.

**How to apply:** For paginated rendered listings, treat any exhausted page fetch as a failed discovery, record failed and not-attempted pages for review, and rerun cleanly when the provider recovers. Never auto-publish a partial catalogue based on overall link count alone.