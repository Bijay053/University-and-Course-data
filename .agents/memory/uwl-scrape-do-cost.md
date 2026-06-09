---
name: UWL Scrape.do cost optimization
description: Why UWL no longer needs headless render, and the id-specific YAML shadowing gotcha
---

# UWL (University of West London) Scrape.do credit reduction

UWL was forcing Scrape.do headless-Chrome render (`render=true`, ~5 credits) on
EVERY fetch: 12 listing pages + ~340 course pages ≈ 1,760 credits per scrape.

**Verified 2026-06-09 against live www.uwl.ac.uk** (curl_cffi + scrape.do):
- Course detail pages are **server-side rendered** — `render=false` returns the
  identical fee/IELTS/duration data as `render=true`. They are even reachable via
  a **free direct curl_cffi fetch** (status 200, full data, no Cloudflare block).
- The `/courses/search?page=N` listing pages are also SSR — `render=false` returns
  the same 57 course links per page as `render=true`.

**Fix:** `extraction.scrape_do_render: false` (course pages → free curl_cffi first,
scrape.do static ~1 credit only as fallback) + new `discovery.render_listing_pages_static: true`
flag (listing pages use scrape.do render=False ~1 credit). Removed `scrape_do_skip_fallbacks`
(it forced straight-to-paid-render, skipping the free path). ~90%+ credit reduction.

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
