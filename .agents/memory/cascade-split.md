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
