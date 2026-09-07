---
name: MQ Funnelback authority + CF bypass
description: Macquarie requires rendered Funnelback discovery plus structured page-data enrichment; bare-link fallbacks expose domestic defaults.
---

## Source authority

Use the international Funnelback catalogue only through the rich MQ provider that retains structured page-data fees, English scores, duration, and evidence. A generic provider that returns only URLs is unsafe because downstream detail pages render the domestic audience by default.

**Why:** A bare-link production run found the full catalogue but staged almost every degree without international fees, while also admitting majors and specialisations as courses. Link-count completeness did not imply data completeness.

**How to apply:** Give the rich provider precedence over caches and generated API recipes. Reject majors and specialisations. Fail the scrape rather than supplementing with domestic HTML when pagination, structured-page coverage, or fee coverage is materially incomplete.

## Transport

Every part of `www.mq.edu.au` and both Funnelback endpoints are behind Cloudflare Enterprise.
- Direct httpx / requests / curl_cffi / Scrapy → CF 403
- scrape.do render=false (static proxy) → 502 ROTATION_FAILED for all MQ URLs
- scrape.do render=true (real Chrome) → ✅ works for both homepage and Funnelback JSON endpoint

**Funnelback endpoint:**
`https://mqu-search.funnelback.squiz.cloud/s/search.json?collection=mqu~sp-courses&profile=international&query=!padrenull`

**Pagination:** 1-based (`start_rank=1` for first result). Server caps at 200 results per request.
- Page 1: `start_rank=1&num_ranks=200` → 200 results
- Page 2: `start_rank=201&num_ranks=200` → ~168 results
- Total: ~368 courses (fullyMatching=368 per resultsSummary)

**Chrome wraps JSON in `<pre>` tag:** When scrape.do render=true opens a JSON URL,
Chrome displays it as `<html><body><pre>{json}</pre></body></html>`.
Rendered JSON fetches must unwrap this before parsing.

**Reference Python spider** (`macquarie_university_au_1786509801112.py`) uses `websearch.mq.edu.au`
and works from non-datacenter IPs (local machines). From Replit servers that endpoint is also CF-blocked.

**Discovery of alternate endpoints tested (all fail from server):**
- `websearch.mq.edu.au` → CF 403 (httpx, requests, curl_cffi, Scrapy all blocked)
- `mqu-search.funnelback.squiz.cloud` → CF 403 direct; ROTATION_FAILED via scrape.do static
- `www.mq.edu.au` sitemap / page-data.json → CF 403 direct; ROTATION_FAILED via scrape.do static
- Wayback CDX → only 73 current course URLs (not enough)

The rendered Funnelback path gets the catalogue in two API calls and avoids the old BFS limit, but it is acceptable only when paired with structured international enrichment and explicit coverage gates.
