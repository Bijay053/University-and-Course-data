---
name: Kingston CF Enterprise discovery fix
description: Kingston (www.kingston.ac.uk) fix for CF Enterprise blocking all datacenter IPs, same pattern as Cardiff but with sitemap timing constraints.
---

# Kingston CF Enterprise Fix (2026-07-08)

## Transport findings
- httpx → 403 on all pages (CF Enterprise, ASN-level block)
- curl_cffi → 403 (same)
- Scrape.do static → 502 (error code 90, ROTATION) on ALL Kingston pages including sitemap
- Scrape.do render=true → 200 ✓ on sitemap AND course pages
- Playwright (datacenter) → also works for per-course browser enrichment (CF Enterprise passes real Chrome UA)

**Why:** http_fetcher.py already has static→render auto-retry on 502. Setting `scrape_do_skip_fallbacks: true` in discovery routes all fetches through Scrape.do static, which then automatically retries with render=true on 502.

## Course page URL structure
Old (invalid): `/courses/{level}/{slug}`
New (correct): `/study/{level}/{slug}` (all 261 courses in sitemap)

## Fee table HTML structure (Numiko/Tailwind template)
```html
<tr><th scope="row">Home (UK students)</th><td>Full Time £13,500</td></tr>
<tr><th scope="row">International</th><td>Full Time £19,700</td></tr>
```
"International" is in `<th>` not `<td>` — old XPath using `td[contains(.,'International')]` returns empty.
Correct XPath: `//tr[th[contains(.,'International') and not(contains(.,'Home'))]]/td[contains(.,'£')]`

## Discovery timeout problem and fix
**Problem:** Each Scrape.do render=true call takes ~60-90s. Discovery budget is 300s.
- Home page probe: ~70s
- BFS auto-detected listing page (course-search): ~70s  
- Sitemap index fetch: ~70s
- Sub-sitemap page=1 (all 261 courses): ~70s
- Sub-sitemap page=2 attempt: budget exceeded → discovery_timeout → job=failed

**Fix:** Point `sitemap_url` directly to `sitemap.xml?page=1` (not the index):
- Skips sitemap index fetch (~70s saved)
- Eliminates page=2 attempt
- Also skips 7 alt-listing-path probes (discovery.py L1596: explicit sitemap_url → _has_explicit_sitemap=True → alt probes skipped)
- Discovery: ~140-210s, comfortably within 300s

**Key insight:** All 261 course URLs are on page=1. Page=2 has 191 non-course entries (blogs, news).

## Why explicit sitemap_url skips alt probes
discovery.py line 1595-1596:
```python
_has_explicit_sitemap = bool(_resolved_sitemap_url)
if len(found) < _ALT_PROBE_THRESHOLD and origin and not _has_explicit_sitemap:
```

## Verified results
- 261 discovered → 254 extractable (7 gated: apply_page, category_landing, info_page)
- 100/100 completeness on tested courses (Gemini + browser fills IELTS, duration)

## Config summary (kingston_2193.yaml)
```yaml
discovery:
  scrape_do_skip_fallbacks: true
  bfs_page_budget: 1
  sitemap_url: https://www.kingston.ac.uk/sitemap.xml?page=1
  always_sitemap_supplement: true
  allow_url_patterns: ['/study/undergraduate/', '/study/postgraduate/', '/study/foundation/']
  use_wayback: false

extraction:
  scrape_do_render: true
  scrape_do_skip_fallbacks: true
  skip_browser_rescue: true
  max_parallel_fetch: 3
```

**Why:** Discovery timeout would hit before sub-sitemap page=1 could be fetched unless the index and page=2 are bypassed.
