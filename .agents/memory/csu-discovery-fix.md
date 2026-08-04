---
name: CSU discovery fix (study.csu.edu.au)
description: study.csu.edu.au is CF Enterprise blocked; Wayback CDX is the only viable discovery path; per-course IELTS not inline.
---

## Rule
`study.csu.edu.au` blocks all static transports (httpx → 403, Scrape.do static → 0B/502).
Scrape.do render=True is the only working transport for both listing and per-course pages.
The course listing (`/courses`) uses virtual scrolling — only 12 of 329 courses appear in the rendered DOM; BFS is useless.
Sitemap is blocked at the transport level (0B from all fetch modes).

**Discovery fix:** `bfs_page_budget: 0` + `skip_browser_discovery: true` + `always_sitemap_supplement: false` → Wayback CDX fallback fires automatically. `study.csu.edu.au` added to `_HOST_CDX_URL_PREFIX` with `study.csu.edu.au/courses/*` (329 unique course URLs confirmed).

**Why:** The default fallback chain (httpx → cffi → scrape.do static → timeout) consumed the entire 300s discovery deadline without returning anything.

**How to apply:** Any university behind CF Enterprise where both the listing page and sitemap are blocked needs this stack:
1. `discovery.scrape_do_skip_fallbacks: true` + `discovery.scrape_do_render: true` (to prevent httpx dead-end)
2. `discovery.bfs_page_budget: 0` if listing uses virtual scroll / JS-only pagination
3. `discovery.skip_browser_discovery: true` (Playwright gets 403 from datacenter IPs)
4. `discovery.always_sitemap_supplement: false` (sitemap blocked)
5. Add host to `wayback_discover._HOST_CDX_URL_PREFIX` with targeted path prefix to avoid SURT-sort exhaustion
6. `extraction.scrape_do_render: true` + `extraction.scrape_do_skip_fallbacks: true` + `skip_browser_rescue: true`

**IELTS gap:** Per-course pages show "Standard ELP requirements apply" (generic, no IELTS scores inline). Actual IELTS requirements link to a separate page. Need to investigate central IELTS page or per-course requirements URL pattern before IELTS data will extract reliably.

**Fees:** Fees are embedded as JSON in the page: `fund_source_type=FPOS, annual_indicative_fee_ft=32000` (international). Gemini extracts these correctly from the rendered HTML.
