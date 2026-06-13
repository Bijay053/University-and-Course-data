---
name: UG/PG split English page routing
description: How to configure separate undergraduate and postgraduate English/IELTS requirement pages so each degree level gets the correct values without cross-contamination.
---

## The problem it solves
When UG and PG requirements live on *different* pages (e.g. University of Law), fetching only one `entryPage` URL means the flat `english` dict carries only one level's values. The existing level-routing code in single_course.py (`_level_bucket` → `english_by_level`) already works correctly — the gap was only in the *source*: only one page was ever fetched.

## Data flow

```
YAML: extraction.english.central_page_ug / central_page_pg
    → orchestrator.py wires → uniPages.entryPageUG / entryPagePG
        → prefetch_central_pages reads both keys
            → fetches each page separately
            → english_by_level["undergraduate"] = UG page parsed slots
            → english_by_level["postgraduate"]  = PG page parsed slots
            → english (flat) = {} (empty — no cross-contamination)
            → english_page_url_ug / english_page_url_pg stored for evidence
                → single_course.py Path 1 uses _level_bucket to pick the right bucket
                → evidence source_url = pg URL for PG courses, ug URL for UG courses
```

## Cache keys
- `english_requirements_ug` — cached independently of PG
- `english_requirements_pg` — cached independently of UG
- Each uses the existing `_cache_get` / `_cache_set` with university_id as partition key.

## Quick Settings UI (db-level override)
`uniPages.entryPageUG` / `entryPagePG` can also be written via the Quick Settings panel (3 separate URL inputs: UG / PG / General) or via PATCH `/api/settings/scraper-configs/{slug}/quick-settings` with `central_english_ug_url` / `central_english_pg_url`.

## Key invariant
When split URLs are configured, the general `entryPage` fetch is **skipped** (english_url is set to None inside prefetch_central_pages). This prevents a single-page flat parse from clobbering the by_level dict built from the split pages.

## Why flat `english = {}` when split
single_course.py has two paths:
- Path 1: `english_by_level[_level_bucket]` — used when per-level data exists
- Path 2: flat `english` — used as fallback

Path 2 always runs after Path 1. If flat english contained UG values (from the general page fetch), Path 2 would apply them to PG courses (where Path 1 already filled the slots correctly — but only for overridable methods). Leaving flat english empty makes Path 2 a no-op for split-page unis.

## University of Law example (law_1902.yaml)
```yaml
extraction:
  english:
    central_page_ug: "https://www.law.ac.uk/study/undergraduate/entry-requirements/"
    central_page_pg: "https://www.law.ac.uk/study/postgraduate/entry-requirements/"
```
No `central_page` needed — when both _ug and _pg are set, the orchestrator populates entryPageUG/PG and the general entryPage is left unset.
