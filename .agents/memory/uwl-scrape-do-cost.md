---
name: UWL Scrape.do cost optimization
description: Why UWL no longer needs headless render, and the id-specific YAML shadowing gotcha
---

# UWL (University of West London) Scrape.do credit reduction

UWL was forcing Scrape.do headless-Chrome render (`render=true`, ~5 credits) on
EVERY fetch: 12 listing pages + ~340 course pages ≈ 1,760 credits per scrape.

**Verified 2026-06-09 against live www.uwl.ac.uk**:
- Course detail pages are **server-side rendered** — Scrape.do `render=false`
  (static residential proxy, ~1 credit) returns the identical fee/IELTS/duration
  data as `render=true`, and is NOT an SPA shell.
- The `/courses/search?page=N` listing pages are also SSR — `render=false` returns
  the same course links per page as `render=true`.
- **In-pipeline, curl_cffi gets 403** (UWL Cloudflare blocks datacenter IPs). A
  one-off standalone curl_cffi can get 200 — do not trust that; the fleet runs from
  datacenter IPs and gets blocked. So the realistic "cheap" path is Scrape.do
  *static* (~1 credit), NOT a free direct fetch.

**Fix:** `extraction.scrape_do_render: false` + new `discovery.render_listing_pages_static: true`
flag. Per-course fetch chain becomes httpx(CF-blocked) → curl_cffi(403) →
Wayback(empty) → Scrape.do static (~1 credit, full page). Listing pages use
Scrape.do render=False (~1 credit). Removed `scrape_do_skip_fallbacks` (it forced
straight-to-paid-render). **~1 credit/page vs ~5 = ~80% reduction.**
Validated live: 2 course pages = 0 render + 2 static calls, full data each.

**Why this matters generally:** "Angular/React SPA" does NOT automatically mean
render is required. Many SPAs server-side render the first paint. Always A/B test
render=false vs render=true (link count + key field presence) before committing a
uni to expensive render mode.

## CRITICAL gotcha — id-specific YAML shadows slug YAML
The config loader prefers `scraper_config/unis/{slug}_{university_id}.yaml` over
`{slug}.yaml`. UWL has BOTH `uwl.yaml` AND `uwl_1881.yaml`. Editing only `uwl.yaml`
has ZERO effect for uni_id=1881 — the id-specific file wins. **When changing a
per-uni config, check for an `{slug}_{id}.yaml` sibling and edit whichever is
active (or both, keeping them in sync).** The id-specific file may carry extra
keys (UWL's has `filters.domestic_only.enabled: true`) the generic one lacks.

`render_listing_pages_static` is a new `DiscoveryConfig` flag (default False →
backward-compatible, keeps render=True for every other uni).
