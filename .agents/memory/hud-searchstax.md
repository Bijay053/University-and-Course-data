---
name: HUD SearchStax provider
description: How and why the University of Huddersfield scraper uses a SearchStax Solr endpoint instead of HTML scraping.
---

## Rule
When a university's live site is a Cloudflare-protected React SPA that the browser
crawler cannot reliably reach, inspect the SPA's network calls — SPAs often query
a hosted search engine (SearchStax Solr, Algolia, etc.) directly from the client.
Querying that search engine bypasses the SPA shell entirely and returns structured
data + full page-text with no Playwright overhead.

**Why:** The `courses.hud.ac.uk/json/...` endpoint that the old Scrapy spider used
now returns an SPA HTML shell. The React SPA itself calls a SearchStax Solr core
(`searchcloud-1-eu-west-2.searchstax.com`) with the `HUD_SEARCHSTAX_TOKEN` secret.
Replicating those Solr calls returns ~790 course docs with all needed fields.

**How to apply:**
- Add a `discovery.searchstax` block in `scraper_config/unis/<slug>.yaml`.
- Implement a provider module (see `searchstax_hud.py`) that paginates Solr and
  returns a list of `{name, url, searchstax_result:{payload, evidence}}` dicts.
- In `orchestrator.py`, when `discovery.searchstax` is configured, call the provider
  instead of browser/BFS and short-circuit `_extract_only()` to return the result verbatim.

## Completeness notes (as of 2026-05-30)
- `_academic_level(degree_level)` — derive Undergraduate/Postgraduate/Doctorate
  from the degree_level string (100% fill rate).
- `_extract_entry_requirement(content)` — regex on Solr `content` field; prefer
  `_DEGREE_REQ_RE` (explicit 2:1/2:2/honours phrases) over `_ENTRY_REQ_RE` anchor;
  reject any match that contains IELTS/English-language keywords via `_LANG_REQ_RE`.
- 280 "errors" per run are year-duplicate dedup rejections (same course in 2026-27
  AND 2027-28 in Solr), NOT crashes. Expected behavior.
- Rate limit (HTTP 429) hits if Solr is called more than once in quick succession
  in dev; the Celery scrape itself is fine (single sequential pass).

## field_map_as_payload type-coercion (Durham, 2026-06-04)
When `cfg.field_map_as_payload` is True, `_map_doc_field_map` copies raw Solr
strings directly into the staged payload — but the payload feeds an INSERT whose
columns are strongly typed. `scraped_courses.duration` is `NUMERIC(6,2)`, so
writing the raw `duration_t` string (e.g. `"3 years full-time"`) caused 299/312
flush failures (`asyncpg DataError: invalid input … decimal ConversionSyntax`).
**Rule:** any numeric-typed column must be parsed before going into the payload
in field_map mode — duration goes through `_parse_duration()` → (value, term, mode),
not the raw string.
**Why:** field_map mode bypasses the normal HTML-extraction path that already
parses these fields, so the coercion must be done inside `_map_doc_field_map`.

## SearchStax + URL filters (Durham, 2026-06-04)
In SearchStax mode the Solr query already filters to course docs
(`fq=sectiontype_ss:course`), so the orchestrator's Phase A.5b
allow/block_url_patterns chokepoint must be skipped (`_skip_url_filters_searchstax`).
**Why:** a stale `admin_config.discovery.allow_url_patterns` in the DB (merged
LAST, and protected by `_LIST_NO_CLEAR_KEYS` so an empty YAML list can't clear it)
silently dropped 53 `/business/courses/` URLs every run. Per-uni YAML cannot
override a DB admin_config list — skip the filter entirely for Solr-sourced links.

## Token
Set `HUD_SEARCHSTAX_TOKEN` in the environment. The YAML has a hardcoded fallback
for dev convenience — never commit a literal token to the YAML in production.
