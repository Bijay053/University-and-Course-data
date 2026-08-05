---
name: CSU discovery fix
description: study.csu.edu.au is CF Enterprise — all transports dead except scrape_do_render; discovery must go straight to Wayback CDX; three YAML knobs skip the wasted probe phases
---

## The problem
Every CSU scrape was burning ~160 s before Wayback CDX fired:
1. CSU-specific `browser_discover_csu_international` (orchestrator line ~1959): hits CF challenge → 20 s networkidle timeout before falling back. Triggered because DB `scrape_url` is `study.csu.edu.au/international/courses`.
2. Alt listing path probes (discovery.py): 7 paths × 20 s each = up to 140 s. Only skipped when `_has_explicit_sitemap` is true.

## The fix (YAML knobs)
```yaml
discovery:
  skip_browser_discovery: true   # skips BOTH generic browser AND csu_browser_discover (orchestrator was patched to check this flag)
  sitemap_url: "https://study.csu.edu.au/sitemap.xml"  # makes _has_explicit_sitemap=True → alt probe loop is skipped
  use_wayback: true              # CDX runs unconditionally (supplemental mode), not just as last resort
  bfs_page_budget: 0
  always_sitemap_supplement: false
```

## Why `sitemap_url` skips alt probes
`discovery.py` line 1782: alt probes only fire when `not _has_explicit_sitemap`. Setting any explicit `sitemap_url` in YAML makes `_resolved_sitemap_url` truthy → `_has_explicit_sitemap = True` → loop is skipped. The sitemap fetch itself still runs (and returns 0B, which is fine).

## Orchestrator patch
The CSU-specific `if not links and "study.csu.edu.au/international/courses" in scrape_url:` block in orchestrator.py was patched to check `skip_browser_discovery` before invoking `browser_discover_csu_international`.

## Wayback CDX coverage
`wayback_discover._HOST_CDX_URL_PREFIX["study.csu.edu.au"] = "study.csu.edu.au/courses/*"` returns ~329 unique course URLs. Sitemap-old-v2 (259 URLs) sometimes works too — use_wayback=True merges both.
