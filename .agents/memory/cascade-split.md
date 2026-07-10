---
name: CASCADE smart split
description: How the post-scrape CASCADE block routes failures to the right self-heal path
---

## The rule
After a scrape completes, the CASCADE block in `run_scrape()` (orchestrator.py) has three branches:

1. **YAML exists** (`_has_yaml`) → log and skip. Operator-tuned configs always win over auto-config.
2. **`staged < 5`** → `[CASCADE:discovery_failure]` → dispatch `probe_and_configure` (re-probe with a different strategy). Discovery is broken.
3. **`staged ≥ 5, avg < 70%`** → `[CASCADE:extraction_failure]` → dispatch `repair_extractor` (Phase 2). Discovery worked; extraction rules are bad.
4. **Otherwise** (staged ≥ 5, avg ≥ 70%) → no action needed.

## Why the split matters
Before Phase 2, both failure modes dispatched `probe_and_configure`. But when discovery is fine (many courses found), re-probing wastes a Gemini call and doesn't fix the underlying extraction problem. The repair path regenerates CSS/XPath/regex rules and re-queues a fresh scrape.

**Why:** Discovery failure and extraction failure have different root causes and require different fixes.

**How to apply:** Whenever you add a new post-scrape quality check, slot it into this three-branch structure. Do not collapse back to a single `_poor` flag.

## Self-inflicted regression trap when adding a new API-style provider
Test runs that fail *before* your fix is fully wired (e.g. before a new custom-API discovery provider was hooked up) still trigger branch 2 and dispatch `probe_and_configure`. That auto-probe writes a fresh `auto_config.discovery.block_url_patterns` into `universities.scrape_config`, generated from whatever the site's real course-catalogue path looks like — which is very likely to overlap with the URL space a same-day custom-API provider is about to start returning.

**Why:** the auto-probe has no awareness of an in-progress hand-written provider; it just infers "these paths look like real course pages, everything else looks like nav/marketing" and blocks the rest.

**How to apply:** after wiring a new custom discovery provider (SearchStax/Swiftype/Manchester-XML/etc.-style), (1) add it to the `_skip_url_filters_searchstax` skip-list in orchestrator.py so admin_config/YAML allow/block_url_patterns don't get applied on top of the provider's own filtering, and (2) clear any stale `auto_config` key from `universities.scrape_config` for that university before re-testing, since earlier failed attempts may have already poisoned it.
