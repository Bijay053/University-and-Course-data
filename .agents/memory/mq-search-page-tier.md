---
name: MQ Funnelback discovery + CF bypass
description: How Macquarie University course discovery works — Funnelback API endpoint, Cloudflare block, scrape.do render=true bypass, pagination.
---

## The working approach (as of Aug 2026)

**Discovery:** `generic_search_api` in mq.yaml with `fetch_via_scrape_do: true` + `scrape_do_render: true`.

**Why:** Every part of `www.mq.edu.au` and both Funnelback endpoints are behind Cloudflare Enterprise.
- Direct httpx / requests / curl_cffi / Scrapy → CF 403
- scrape.do render=false (static proxy) → 502 ROTATION_FAILED for all MQ URLs
- scrape.do render=true (real Chrome) → ✅ works for both homepage and Funnelback JSON endpoint

**Funnelback endpoint:**
`https://mqu-search.funnelback.squiz.cloud/s/search.json?collection=mqu~sp-courses&profile=international&query=!padrenull`

**Pagination:** 1-based (`start_rank=1` for first result). Server caps at 200 results per request.
- Page 1: `start_rank=1&num_ranks=200` → 200 results
- Page 2: `start_rank=201&num_ranks=200` → ~168 results
- Total: ~368 courses (fullyMatching=368 per resultsSummary)

**YAML knobs used:**
```yaml
fetch_via_scrape_do: true
scrape_do_render: true
page_size: 200
offset_param: start_rank
offset_start: 1        # 1-based Funnelback
```

**Chrome wraps JSON in `<pre>` tag:** When scrape.do render=true opens a JSON URL,
Chrome displays it as `<html><body><pre>{json}</pre></body></html>`.
`fetch_yaml_api_links` calls `_unwrap_chrome_json()` to extract the raw JSON before parsing.

**Reference Python spider** (`macquarie_university_au_1786509801112.py`) uses `websearch.mq.edu.au`
and works from non-datacenter IPs (local machines). From Replit servers that endpoint is also CF-blocked.

**Discovery of alternate endpoints tested (all fail from server):**
- `websearch.mq.edu.au` → CF 403 (httpx, requests, curl_cffi, Scrapy all blocked)
- `mqu-search.funnelback.squiz.cloud` → CF 403 direct; ROTATION_FAILED via scrape.do static
- `www.mq.edu.au` sitemap / page-data.json → CF 403 direct; ROTATION_FAILED via scrape.do static
- Wayback CDX → only 73 current course URLs (not enough)

**Why:**
`generic_search_api` with `fetch_via_scrape_do + scrape_do_render` gets all 368 courses cleanly
in 2 API calls, far better than the old BFS crawl via scrape.do that was capped at ~200 courses
(BFS page budget hit before all courses found).
