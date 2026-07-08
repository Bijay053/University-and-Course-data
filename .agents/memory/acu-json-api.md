---
name: ACU JSON-API discovery + title-suffix
description: ACU (www.acu.edu.au) Vue SPA catalogue via /webapi JSON API; course pages have no H1 so <title> suffix must be stripped
---

# ACU (Australian Catholic University) scraping quirks

- **Discovery**: find-a-course grid is a Vue SPA with ZERO server-rendered course links. The catalogue comes from `/webapi/GetCourseResult/get?CourseType=<slice>&sr=<Sitecore GUID>` — 4 slices (Undergraduate/Postgraduate/Research/Other), each returns its full slice unpaginated. The `/webapi/` endpoint is NOT Cloudflare-blocked for plain httpx, while page/sitemap fetches ARE (needs `scrape_do_skip_fallbacks: true`).
  **Why:** BFS/browser discovery can never see the catalogue; empty CourseType returns fewer URLs than the 4-slice union.
- **API JSON has UTF-8 BOM** — decode with `utf-8-sig` when inspecting manually.
- **Course pages have NO `<h1>` in static HTML** — the only course-name candidate is the page `<title>`, which carries the CMS suffix `… | ACU courses`. Fixed via `extraction.course_name.strip_title_suffixes` in `acu.yaml` (endswith-literal, applied to RAW title pre-clean, so list must include exact-case variants).
  **How to apply:** any uni whose staged names all share a trailing `| <site name>` string and whose pages lack an H1 → same YAML knob; API/link names being clean does NOT help because extraction re-derives the name from the page.
- **Verified run (dev 2026-07-08)**: 212 links found, 116 staged (rest = Domestic/International URL-variant dedup), 0 errors, avg completeness 90%, 113/116 ≥85% auto-publish floor, Gemini ~$0.11/run.
