---
name: block_url_patterns traversal guard
description: block_url_patterns is now ALSO a BFS traversal-level pre-fetch guard, not only a post-BFS candidate filter. Critical for Scrape.do-backed discovery.
---

## Rule

`discovery.block_url_patterns` in YAML is applied at TWO points:

1. **Post-BFS candidate filter** (always existed): after the BFS loop completes, any
   candidate URL matching a block pattern is dropped from the `found` dict.

2. **BFS traversal-level pre-fetch guard** (added 2026-07-10): patterns are compiled
   early (before the BFS loop) and checked against each dequeued URL BEFORE fetching it.
   If a depth>0 URL matches any block pattern, BFS skips it (`continue`) without making
   a network call. `allow_url_patterns` overrides still take precedence.

## Why this matters for Scrape.do-backed discovery

Each BFS page fetch costs ~6s when render=true is required (Scrape.do). Without the
traversal guard, BFS fetches nav/non-course pages found as links on legitimate course
pages (e.g. ECU's /future-students/*, /study/events, /degrees/study-areas/*), consuming
~90s of the 300s discovery budget on pages the operator explicitly blocked. With the
guard, those pages are skipped at zero cost.

## How to apply

Add `block_url_patterns` to the university's YAML as normal. The traversal guard fires
automatically — no additional config needed. Depth-0 seeds are never blocked by
traversal patterns (seeds bypass all block checks).

## Code location

`backend-py/app/services/scraper/discovery.py`:
- Early compilation: `_yaml_block_compiled_early` (set up before BFS loop).
- Traversal check: after the `is_blocked_page` block, before `_bounded_fetch`.
- Post-BFS filter: `_compiled_block` block (unchanged, ~line 1940 after the addition).
