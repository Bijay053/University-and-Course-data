---
name: httpx params={} strips URL query string
description: Passing params={} (empty dict) to httpx replaces/removes an existing query string already embedded in the URL.
---

# httpx params={} strips the URL's own query string

**Rule**: When a URL already carries its query string (e.g. YAML-configured
API URLs like ACU's `/webapi/GetCourseResult/get?CourseType=X&sr=<guid>`),
pass `params=my_params or None` to httpx — never an empty dict.

**Why:** httpx treats any non-None `params` as the authoritative query and
rebuilds the URL from it. `params={}` therefore silently deletes the query
string that was part of the URL, and the API returns wrong/empty results
with no error. Found in the generic_search_api provider: configured
additional_urls lost their `CourseType`/`sr` parameters and every slice
returned the same default result set.

**How to apply:** In any fetch helper that accepts optional request params
and URLs that may embed queries, use `params=req_params or None`.
