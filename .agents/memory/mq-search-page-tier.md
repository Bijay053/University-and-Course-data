---
name: MQ Funnelback API discovery
description: MQ primary discovery is now Tier 0 — Funnelback JSON API returns all 338+ courses with real liveUrls + page-data.json for fees/IELTS. Replaces coursehandbook sitemap + search-page browser approach.
---

# MQ Discovery — Funnelback JSON API (Tier 0)

## What Changed (August 2026)
The coursehandbook sitemap resolver (old Tier 1) constructed `/courses/<slug>` URLs that 404'd for
40-47% of courses whose real paths are `/undergraduate/`, `/postgraduate/`, or `/research/`. This
was fixed by replacing both tiers with a Funnelback JSON API call.

## Funnelback API (Tier 0)
**URL**: `https://mqu-search.funnelback.squiz.cloud/s/search.json?collection=mqu~sp-courses&profile=international&query=!padrenull&start_rank=1&num_ranks=500`

Returns 338+ courses with:
- `liveUrl` — real admissions URL (no slug construction or resolver needed)
- `title` — course name
- `metaData.studyLevel` — "Undergraduate" / "Postgraduate"
- `metaData.courseDuration` — "2 years" etc.

The endpoint is CF-protected from Replit's IP; fetched via `scrape.do render=False` (residential
proxy), with httpx fallback for production IPs that aren't blocked.

## page-data.json Extraction
Each `liveUrl` maps to a Gatsby page-data.json endpoint:
- `https://www.mq.edu.au/study/find-a-course/undergraduate/bachelor-of-arts`
- → `https://www.mq.edu.au/study/page-data/find-a-course/undergraduate/bachelor-of-arts/page-data.json`
- Transform: replace `.au/study/` with `.au/study/page-data/`, append `/page-data.json`

JSON path: `result.data.current.fields.json` (a JSON-encoded string → parse again).

Fields extracted:
- `fees[].estimated_annual_fee` where `fee_type.label` contains "international" → `international_fee`
- `ielts_overall_score`, `ielts_reading_score`, `ielts_writing_score`, `ielts_listening_score`, `ielts_speaking_score`
- `marketing_items.descriptions[].long_description` → `description`
- `admission_requirements` → `other_requirement`
- `offering[].location` → `course_location` + `study_mode` ("Off-campus" = Online)
- `enrolment_patterns` → `study_load` ("Full Time" / "Part Time")

## scrapy_result Short-circuit
Each link returned by Tier 0 carries a `scrapy_result` key. `orchestrator._extract_only` returns
this verbatim — no per-course HTTP fetch, no HTML extraction pipeline runs.

## Flow in browser_discover_mq()
- If Tier 0 returns ≥ 300 courses → return immediately, skip all other tiers
- If Tier 0 returns < 300 → supplement with Tier 1 (coursehandbook sitemap) + Tier 1.5 (search page)
- The Tier 0 links' `scrapy_result` wins over Tier 1's URL-only entries via `setdefault`

## YAML Change
`failure_guard_threshold` lowered from 0.55 → 0.30 (Funnelback provides real liveUrls; expected
fetch_failed rate < 5% vs the old 40-47%).

## Code Location
`backend-py/app/services/scraper/mq_browser_discover.py`:
- `_page_data_url()` — URL transform
- `_extract_program_from_page_data()` — parse page-data.json body
- `_build_scrapy_result()` — build payload + evidence from Funnelback meta + program dict
- `_discover_from_funnelback_api()` — orchestrates the whole Tier 0 flow
- `browser_discover_mq()` — wires Tier 0 before Tier 1

## How to Apply
If MQ staging count drops below 300 on the next run, check the job log for:
- `[DISCOVER] MQ: Tier 0 — Funnelback returned N courses` — if N < 50, the API is unreachable
- `[DISCOVER] MQ: Tier 0 — page-data.json via plain httpx: N/338` — if N is low, CF is blocking
  page-data.json; the scrape.do retry path should cover the remainder
- If Funnelback URL changes, check MQ's website search source to find the new collection name
