---
name: WLV SearchStax provider
description: Wolverhampton uses SearchStax Solr (433 courses); switched to field_map_as_payload=true after scrape.do removal made links_only mode 83-min slow.
---

## Rule
WLV now uses `field_map_as_payload: true` — courses built directly from Solr fields, **zero per-course page fetches**. Do NOT revert to `links_only: true` unless scrape.do (or equivalent paid proxy) is available again.

**Why:** WLV's course pages are 100% Cloudflare-blocked. With scrape.do removed, `links_only: true` sent all 432 URLs through httpx→curl_cffi→Wayback each failing after ~15s, giving an 83-minute runtime. Switching to `field_map_as_payload` makes the run complete in <60s.

**How to apply:** Any WLV scrape config change must keep `links_only: false` + `field_map_as_payload: true`. Only switch back to `links_only` if a residential proxy tier is restored.

## WLV Solr field map (verified 2026-06-04)
Standard defaults match WLV for `url` (url_t), `name` (title_t), `degree_type` (award_s).
Override required for:
- `degree_level`  → `level_s`
- `study_mode`    → `multi_mode_ss`
- `duration`      → `multi_duration_ss`
- `intake_dates`  → `multi_course_start_date_ss`
- `category`      → `subject_area_ss`
- `location`      → `multi_location_ss`  (new key added to _map_doc_field_map)

## Fee/IELTS defaults (no Solr data for these)
- `degree_level_defaults: {undergraduate: 17600, postgraduate: 17600}` (£17,600 flat rate)
- `default_ielts: 6.0`

## SearchStax endpoint details
- URL: `https://searchcloud-1-eu-west-2.searchstax.com/29847/wolverhamptondevelopment-3254/emselect`
- Filter: `sectionType_s:courses`
- Auth: `WLV_SEARCHSTAX_TOKEN` env var
- Total: ~433 courses

## `location` key in field_map (new, 2026-06-04)
`_map_doc_field_map` in `searchstax_hud.py` now supports a `location` field_map key.
- No built-in default (omitted if key not set in YAML)
- Multi-valued Solr lists are joined with `", "` into `course_location`
- `location_override` still wins over `location` from field_map if both are set
