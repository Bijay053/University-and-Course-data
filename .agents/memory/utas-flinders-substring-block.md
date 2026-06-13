---
name: UTAS/Flinders substring block
description: Global _NON_COURSE_URL_PATTERNS and is_blocked_page() substring entries for /study/undergraduate and /study/postgraduate were added for UTAS/Flinders but block any other university using the same URL prefix scheme.
---

## The rule

`/study/undergraduate` (no trailing slash, substring match) and `/study/postgraduate/` (with slash) are in:
1. `discovery.py` `_NON_COURSE_URL_PATTERNS` — blocks sitemap parser (`_is_known_non_course_url()`)
2. `guards.py` `_BLOCK_URL_SUBSTRINGS` — blocks staging gate (`is_blocked_page()`)

## Why it exists

- UTAS discipline hub: `/study/undergraduate/` is a subject-area category page, not a course
- Flinders category hub: `/study/postgraduate/` lists all PG courses (not individual detail pages)

## The fix pattern (same as ARU)

When a university publishes real course detail pages under these path prefixes, add the hostname to BOTH exception dicts:

```python
# discovery.py
_NON_COURSE_URL_HOST_EXCEPTIONS = {
    "www.example.ac.uk": frozenset({"/study/undergraduate", "/study/postgraduate"}),
}

# guards.py
_BLOCK_URL_SUBSTRINGS_HOST_EXCEPTIONS = {
    "www.example.ac.uk": frozenset({"/study/undergraduate", "/study/postgraduate/"}),
}
```

Note the subtle difference: discovery uses `/study/postgraduate` (no trailing slash), guards uses `/study/postgraduate/` (with slash). Match the existing entry exactly.

## YAML companion

Also set in the university's YAML:
```yaml
discovery:
  allow_url_patterns:
    - /study/(?:undergraduate|postgraduate)/[^/]+/[^/]+/
  course_detail_url_patterns:
    - /study/undergraduate/[^/]+/[^/]+/
    - /study/postgraduate/[^/]+/[^/]+/
```

`allow_url_patterns` bypasses `is_blocked_page()` in BFS discovery but NOT in the sitemap parser (which calls `_is_known_non_course_url()` directly). The code fix is required for sitemap. `course_detail_url_patterns` triggers `skip_url_block=True` in `stage_course.py` for defence-in-depth.

## Affected universities found so far

- ARU (www.aru.ac.uk) — added first
- University of Law (www.law.ac.uk, uni_id=1902) — 175 courses blocked by UTAS pattern; fixed same way

**Why:**  The substring patterns were added for specific universities but expressed as global rules with no host scoping, so they collide with any university that happens to use the same URL prefix convention.
