---
name: UWE-style card anchors defeat text-based course heuristics
description: When a listing page's <a> wraps the whole card (badge + name + metadata), anchor-text length/qualifier checks reject nearly all real course links; use force_candidate_url_patterns keyed on URL shape instead.
---

Some university catalogue templates put the ENTIRE result card — badge/type
label, course name, course code, duration, delivery mode — inside a single
`<a>` tag, rather than just the course name. Once whitespace is normalized,
the flattened anchor text is a 200-400+ char blob. `_looks_like_course()`'s
`_MAX_COURSE_NAME_LEN` cutoff (120 chars) and its degree-qualifier text match
both fail against this blob almost every time — only the rare card whose
badge itself happens to contain a qualifier word (e.g. "BA(Hons)") slips
through by accident. If the course URL also doesn't match any generic
`_COURSE_URL_HINTS` substring (e.g. it's shaped `/<CODE>/<slug>` rather than
`/courses/<slug>`) and isn't `_is_category_landing()`-shaped either, the link
is silently dropped — never staged, never even queued for drill-in.

**Why:** the generic BFS classifier is fundamentally text-token-driven; it
has no way to know a given site's card markup interleaves multiple data
points inside one anchor.

**How to apply:** don't try to make the text heuristic smarter (fragile,
site-specific). Instead declare the URL SHAPE authoritative via YAML
`discovery.force_candidate_url_patterns` — a regex matching only that
site's course-URL shape (e.g. `^/[A-Z][A-Z0-9]{2,10}/[a-z0-9-]+/?$` for
UWE's course-code-then-slug paths). The BFS legacy-link-sweep in
`discovery.py` now checks `force_candidate_url_patterns` BEFORE calling
`_looks_like_course()`, and when matched, derives the candidate name from
the URL slug (never from the noisy anchor text) rather than skipping the
link. This mirrors the existing (narrower) force-candidate use for
category-landing-shaped URLs, but fires unconditionally on any anchor —
not just ones `_is_category_landing()` already flagged — since UWE's course
URLs don't hit that shape check at all.
