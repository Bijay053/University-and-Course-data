---
name: UWL rendered listing coverage
description: Intermittent provider failures can omit nonconsecutive listing pages despite successful static-to-rendered failover.
---

Rendered access working is not, by itself, proof that a paginated catalogue is complete. A provider can return HTTP 502 for nonconsecutive rendered listing pages while successfully rendering the pages around them; the current consecutive-failure guard does not treat that as a fatal discovery error. Check each configured listing page's outcome as well as the aggregate URL count before trusting a full-catalogue run.

**Why:** A live University of West London attempt switched successfully from static to rendered access but missed two nonconsecutive pages and discovered fewer URLs than its configured floor. A fresh attempt loaded all pages and cleared the floor. A single count threshold might miss this class of failure if enough unrelated URLs inflate the total.

**How to apply:** For paginated rendered listings, require an explicit success record for every configured page in full-catalogue verification. Keep any uncertain output in review; do not publish a partial catalogue based on overall link count alone.