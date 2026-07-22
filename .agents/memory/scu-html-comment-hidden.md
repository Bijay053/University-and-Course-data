---
name: SCU HTML-comment hidden URLs
description: SCU listing page buries 122 base course URLs inside an unclosed HTML comment; fix is accepting year-versioned URLs instead.
---

## The rule
SCU's listing page (`https://www.scu.edu.au/study/courses/`) has its 122 base course `<a>` links (e.g. `.../slug-1007289/`) inside an HTML comment spanning positions ~81895–96977:

```html
<!-- Close
<section class="masthead-no-image ...">
  <ul class="list-unstyled"><li><a href=".../slug/">code</a></li>...
</section>
Default -->
```

Python's `HTMLParser` treats everything inside `<!-- ... -->` as comment text and emits **no** `handle_starttag` events. `_LinkExtractor` therefore finds **zero** base course URLs no matter what `force_candidate_url_patterns` you add — the hrefs never reach the BFS loop.

Year-versioned URLs (`.../slug/2026/`, `.../slug/2027/`) appear **after** position ~97000, after the comment closes, and ARE extracted correctly.

**Why:** `force_candidate_url_patterns` only upgrades links that the BFS already observed via `_LinkExtractor`; it cannot manufacture links from content inside HTML comments.

## How to apply
- Do NOT use `force_candidate_url_patterns` for URLs hidden in HTML comments on other sites — the pattern never matches anything.
- When a university has both base and year-versioned URLs: check whether the BFS actually sees the base URLs in `ext.links` before adding force-candidate patterns.
- For SCU: the DB recipe (`scrape_config.recipe`) must have `block_url_patterns: []` and `course_year.preferred_year: 2027` (2027 URLs are the primary discoverable set). `fee_reject_years` should also be empty. The `slug_without_year` dedup then collapses 2026/2027 pairs to a single course entry.
- Discovery URL cache must be cleared (`DELETE FROM discovery_url_cache WHERE university_id=<N>`) after fixing the recipe so the next run re-runs BFS.

## Verification
Binary-search the corruption point in `html[:X] + snippet` tests. Confirmed with SCU at position 81875–81906 (`<!-- Close\r\n...`). The corruption shows as oscillation (some prefixes broken, others fixed) as comments open and close throughout the page — the final state at position 82939 is broken because the comment at 81895 is still open.
