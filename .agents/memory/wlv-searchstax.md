---
name: WLV SearchStax provider
description: Wolverhampton uses SearchStax Solr for discovery (433 courses) but Solr docs lack fees/IELTS — links_only mode needed.
---

## Rule
WLV's SearchStax Solr core indexes 433 courses but does NOT include fees or IELTS scores in the docs (unlike Huddersfield which has the full `content` field). Use `links_only: true` in the YAML so SearchStax provides URL discovery only and per-course browser+Gemini extraction runs normally for each URL.

**Why:** Browser BFS only found 93/433 courses (Cloudflare blocks listing pages + 180s budget exhausted). Solr gives the complete catalogue. But because Solr lacks fees/IELTS, the full HUD short-circuit approach (returning `searchstax_result` verbatim) would produce incomplete staged courses.

**How to apply:** Any UK university using SearchStax whose Solr docs lack fees/IELTS — add `discovery.searchstax.links_only: true`. For universities where Solr has full page-text content (like HUD), use `links_only: false` (default) to short-circuit per-course fetching.

## Endpoint details
- Solr URL: `https://searchcloud-1-eu-west-2.searchstax.com/29847/wolverhamptondevelopment-3254/emselect`
- Filter: `sectionType_s:courses`
- Auth: `WLV_SEARCHSTAX_TOKEN` env var (read-only token shipped to every browser)
- Fields available: `title_t`, `award_s`, `url_t`, `level_s`, `multi_course_start_date_ss`, `multi_duration_ss`, `multi_mode_ss`, `multi_location_ss`, `description_t`, `subject_area_ss`
- Total: 433 courses (verified 2026-06-04)

## How to intercept SearchStax token for any university
Use Playwright network intercept targeting `searchstax` + `29847` in request URL. The token appears in the `Authorization: Token <t>` header. WLV token was found via: `page.on('request', ...)` with `asyncio.sleep(15)` wait after `domcontentloaded`.
