---
name: Elastic bootstrap _from_item NameError
description: elastic_api_bootstrap in browser_discover_generic.py silently fails with 0 courses because _from_item is a closure inside _extract_courses_from_xhr_json and is not in scope from the bootstrap's outer context.
---

## The Rule

Never call `_from_item()` from the bootstrap code. It is defined inside `_extract_courses_from_xhr_json()` (indent 8) and is NOT accessible at the bootstrap's scope (indent 48 in the outer async function). Call `_extract_courses_from_xhr_json(_eas_data)` instead and dedup the returned list against `_eas_seen_urls`.

**Why:** Python closures have function-level scope. `_from_item` defined inside `_extract_courses_from_xhr_json` is only accessible within that function's body. Calling it from the bootstrap raises `NameError: name '_from_item' is not defined`. This is caught silently by the per-page `except Exception as _eas_exc: break` handler, causing 0 courses from all queries and no error in the emit log.

**How to apply:** Whenever the bootstrap needs to process an Elastic API JSON response page, pass the full response dict to `_extract_courses_from_xhr_json(_eas_data)`, iterate the returned list of `{url, name}` dicts, dedup against `_eas_seen_urls`, and append to `results`.

## Symptom

Log shows:
```
[DISCOVER] Elastic API q='bachelor': total_results=525 total_pages=6
[DISCOVER] Elastic API query: 'master'       ← next query starts immediately, no per-page result logged
[DISCOVER] XHR API hit: ... → +39 course(s) ← XHR interceptor captures first response instead
[DISCOVER] Seed visited: ... → +0 courses (total=0)
[DISCOVER] XHR merge: +39 course(s) from 1 JSON API endpoint(s)
```

No `[DISCOVER] Elastic API q='bachelor' page 1/6 → +N new courses` line appears between the total_results log and the next query. This is the tell: the per-page emit fires AFTER `_from_item`, so if `_from_item` throws, the emit never fires.

## Fixed in

`backend-py/app/services/scraper/browser_discover_generic.py` lines 1074–1091 — replaced broken per-item `_from_item` loop with `_extract_courses_from_xhr_json(_eas_data)` call.
