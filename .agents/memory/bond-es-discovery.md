---
name: Bond University Elasticsearch discovery
description: Bond scraper uses the open ES API; sitemap 403s and program-finder SPA returns 0 links.
---

## Rule
Use `discovery.generic_search_api` with the Bond internal Elasticsearch endpoint.  
Do NOT rely on sitemap supplement or program-finder BFS — both are broken.

**Why:**
- `bond.edu.au/sitemap.xml?page=N` (all child pages) returns HTTP 403 from datacenter IPs — supplement produces 0 URLs.  
- `bond.edu.au/study/program-finder` is a React SPA; XHR fires after Scrape.do's render window, so even `scrape_do_render: true` returns 0 course-card anchors. Only ~13 static featured cards on the homepage were discovered → ~11 staged (observed in production August 2026).  
- The ES index is open (no auth) and returns all 221 international courses in a single POST with a browser-like User-Agent. httpx with `Accept: application/json` header passes the CF check.

**How to apply:**
```yaml
generic_search_api:
  enabled: true
  method: POST
  url: "https://bond.edu.au/api/v1/elasticsearch/bond_prod_default/_search"
  headers:
    content-type: "application/json"
  body:
    query:
      term:
        student_type: "International students"
    size: 500
    _source: [url, title]
  root_path: "hits.hits"
  url_fields: ["_source.url.0"]       # url is a list; .0 unwraps first element
  title_fields: ["_source.title.0"]
  base_url: "https://bond.edu.au"
  normalize_relative_urls: true
  allow_url_patterns:
    - '^https://bond\.edu\.au/program/[^/]+/?$'
    - '^https://bond\.edu\.au/microcredential/[^/]+/?$'
```

**Results as of August 2026:** 221 hits, 220 pass allow filter (168 `/program/` + 52 `/microcredential/`). One stray `/translational-simulation` URL is filtered by `allow_url_patterns`.

**If the index name changes:** check for the updated endpoint path in the program-finder rendered HTML `data-` attributes.

**If the ES API starts requiring auth:** fall back to the sitemap approach (if CF removes the 403 on child pages) or the program-finder seed approach with a longer wait_for.
