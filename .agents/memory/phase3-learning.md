---
name: Phase 3 learning layer
description: scraper_patterns table, pattern_store.py, and all four hookpoints for the autonomous learning flywheel
---

## The rule
Phase 3 adds a learning flywheel via the `scraper_patterns` table:
- When a repair succeeds, `promote_patterns()` upserts rules keyed by `(platform_type, field_key)`
- When a university is probed, `lookup_patterns()` seeds the Gemini prompt with proven rules
- Result: university #N on WordPress starts with rules proven on universities #1…#N-1

## Platform type derivation — `derive_platform_type(profile)`
Priority (highest → lowest):
1. `profile.detected_apis[0].provider` — most specific (searchstax, algolia, solr)
2. `profile.library_stack.situation` — CMS type (wordpress, drupal, terminalfour)
3. `profile.recommended_strategy` — coarse fallback (sitemap_first, browser, wayback)

This same logic is duplicated inline in `probe_and_configure` (scrape_tasks.py) to avoid importing `auto_config_generator` there.

## Hookpoints
1. `probe_and_configure` (Stage 2b): compute platform_type from profile → `lookup_patterns()` → pass `learned_patterns` to `generate_config()` → passed to `generate_and_store_rules()`
2. `generate_and_store_rules()`: if `learned_patterns`, inject `_build_seeded_prompt()` between `_SYSTEM_PROMPT` and `_USER_TEMPLATE`; Gemini wins on overlap; learned backfills uncovered fields; if Gemini skipped, learned stored as-is (`_extraction_rules_source="learned_fallback"`)
3. `repair_extractor` (after apply_repaired_rules_to_db): fetch `_platform_type` from `auto_config` → `promote_patterns()` with `REPAIR_ESTIMATED_FILL_RATE=0.75`

## Promotion threshold
- `PROMOTE_MIN_FILL_RATE = 0.70` — only fields with ≥70% fill qualify
- `REPAIR_ESTIMATED_FILL_RATE = 0.75` — optimistic seed used at repair time (before rescrape runs); must be ≥ PROMOTE_MIN_FILL_RATE or repairs are never promoted
- Running average update on upsert conflict: `(old_avg * old_count + new_rate) / (old_count + 1)`

## apply_repaired_rules_to_db arg order
Signature: `apply_repaired_rules_to_db(university_id, repaired_rules, db)` — university_id FIRST.
The Celery task previously called it as `(db, university_id, new_rules)` — wrong order, now fixed.

**Why:** Pattern store lookup must be non-fatal — a DB outage should not block probe from running. All calls wrapped in `try/except`, return empty dict on error.

**How to apply:** Any new Celery task that calls `repair_extractor` or `probe_and_configure` flow automatically benefits. No opt-in required.
