---
name: Leeds Trinity SearchStax discovery
description: Leeds Trinity's course listing is a SearchStax Search Studio widget; links_only mode verified 134/134 courses.
---

## Rule
Leeds Trinity (uni_id=2220) uses `links_only: true` (like WLV), NOT `field_map_as_payload` or the HUD full-payload mode — its Solr docs have no fee/IELTS content field, only metadata, so per-course pages are still fetched normally for extraction.

**Why:** Confirmed via direct Solr query that `numFound=134` exactly matches the user-reported course count with zero title exclusions, so no extra filtering is needed beyond the default `sectionType_s:"courses"` fq + `model=courses` extra param.

**How to apply:** When a university's course-search page renders no HTML cards and loads `static.searchstax.com/studio-js/v3/js/studio-app.js`, extract the `studioConfig.connector` object from page source (`url`, `select_auth_token`) rather than guessing endpoints — `select_auth_token` (not `searchAPIKey`) is the working `Authorization: Token` value.

## Endpoint details
- URL: `https://searchcloud-1-eu-west-2.searchstax.com/29847/ltu-1638/emselect`
- `filter_query: 'sectionType_s:"courses"'` (quoted value form, not default `sectionType_s:course`)
- `extra_params: {model: courses}` — required or the core falls back to a different default model (`LeedsTrinity`) with unrelated boost/blocklist params
- `url_fields: [url]`, `title_fields: [CourseTitle_t]` (generic mapper, `use_generic_mapper: true`)
- Auth token stored as plain env var `LEEDSTRINITY_SEARCHSTAX_TOKEN` (not a Replit secret — same "public SPA token" reasoning as HUD/WLV)
