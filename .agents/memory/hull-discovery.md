---
name: Hull discovery (3 static listing pages)
description: University of Hull scraper config — three static listing pages, no CF, no render needed, ~471 total courses.
---

## Rule

University of Hull (www.hull.ac.uk) — no Cloudflare protection, all course listings
are complete in static HTML. Three listing pages each cover one level:

| Page | Courses |
|------|---------|
| /study/undergraduate/courses | ~248 UG slugs |
| /study/postgraduate-taught/courses | ~159 PG-Taught slugs |
| /study/postgraduate-research/courses | ~64 PG-Research slugs |

## Discovery

Set all three as `seed_urls`, `bfs_page_budget: 3` (no further BFS traversal needed),
`scrape_do_render: false`, `always_sitemap_supplement: false`.

## Extraction

Course detail pages are ~560 KB SSR payloads. Fee, IELTS, duration, and study mode
are all present in the embedded server-side JSON blob. Plain httpx retrieves everything.
No Playwright, no Scrape.do, no browser rescue.

```yaml
extraction:
  scrape_do_render: false
  skip_per_course_browser: true
  skip_browser_rescue: true
  max_parallel_fetch: 6
```

## DB Note

As of 2026-07-09, www.hull.ac.uk does NOT have a universities record in the DB.
Only "University of Hull (London)" at london.hull.ac.uk exists (id=2227).
The YAML file (hull.yaml) is ready but a new DB record with
`scrape_url='https://www.hull.ac.uk/'` must be inserted before running a scrape.

**Why:** The loader derives slug "hull" from www.hull.ac.uk and looks up hull.yaml.
Without a DB record the scraper has no university_id to assign scraped courses to.
