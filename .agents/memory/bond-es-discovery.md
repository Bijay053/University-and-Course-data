---
name: Bond University Elasticsearch discovery
description: Bond's program finder is a React SPA; course URLs come from an internal Elasticsearch API, not BFS/browser crawling.
---

## Rule
Use `discovery.generic_search_api` with POST to `https://bond.edu.au/api/v1/elasticsearch/bond_prod_default/_search`.

**Why:** The program finder (bond.edu.au/study/program-finder) is a React SPA that makes AJAX calls to the ES endpoint after page load. Plain HTTP BFS returns 0 links. Even browser-rendered scrape.do fetches return 0 links because the XHR fires after the initial render window.

## Key implementation details
- ES response shape: `hits.hits[*]._source.url[0]` / `.title[0]` (array-valued fields)
- Use `_source.url.0` in url_fields — `_dig()` handles numeric-index dot-paths to unwrap list[0]
- Filter: `{"term": {"student_type": "International students"}}` — returns ~221 results (168 /program/ + 52 /microcredential/)
- `size: 500` fetches all in one request; no pagination needed
- `allow_url_patterns: ['^https://bond\.edu\.au/program/', '^https://bond\.edu\.au/microcredential/']`
- Course pages (bond.edu.au/program/<slug>) are SSR-accessible via plain HTTP (~256KB each), so per-course extraction works normally

## How to apply
If Bond's ES index changes, query `https://bond.edu.au/api/v1/elasticsearch/bond_prod_default/_search` with `{"query":{"match_all":{}},"size":0}` to check the total doc count. If the index name changes, check the `data-` attribute in the program finder rendered HTML for the updated endpoint path.
