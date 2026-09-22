---
name: WLV SearchStax provider
description: Wolverhampton uses SearchStax Solr for course-owned links; provider URLs must bypass stale post-discovery filters.
---

## Rule
Do not enable WLV's SearchStax route in production based only on recipe tests:
verify that the running environment resolves its configured authentication and
that a live query returns course records.

**Why:** Enabling the canonical recipe without its production token produced
HTTP 401 and zero provider candidates despite passing configuration tests.

**How to apply:** Check credential existence without exposing values, then test
the authenticated provider request before claiming catalogue recovery. A healthy
release and a correct field map do not establish provider availability.

WLV uses SearchStax as the authority for which URLs are courses. Provider-owned
links must bypass every post-discovery URL filter, including the final course
detail allowlist. WLV may use links-only mode when a reliable static proxy is
available, or payload mode when detail-page transport is unavailable.

**Why:** Without a static proxy, links-only mode once sent hundreds of blocked
pages through slow fallbacks. After static transport was restored, links-only
mode became useful again for richer page fields. Separately, a stale
operator/generated course-detail allowlist rejected every valid provider URL,
turning a healthy provider result into a zero-course scrape.

**How to apply:** Choose links-only versus payload mode from current verified
detail-page transport, not old assumptions. In either mode, validate the
provider's document type and URL ownership before emitting links, then do not
apply BFS/sitemap allow, block, must-contain, or detail patterns to that trusted
set. Provider-side title/type exclusions remain valid.

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
