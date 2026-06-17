---
name: Strath research discovery fix
description: Two YAML bugs that blocked all 53 Strathclyde research degrees; H1 span selector lesson.
---

## The bugs

### Bug 1 — wrong block pattern scope
`block_url_patterns: ["/research/"]` is a regex applied with `.search()` on the full URL.
It fires on `https://www.strath.ac.uk/courses/research/chemistry/` because `/research/` appears in the path.
**Fix:** use `"strath.ac.uk/research/"` — only matches when `/research/` follows the hostname directly.

### Bug 2 — wrong PGR path
Strath uses `/courses/research/{slug}/` for research degrees (PhD, MPhil, MRes, DBA).
The path `/courses/postgraduateresearch/` **does not exist** on this site.
Our allow_url_patterns and force_detail_pages had the dead path; research courses never became candidates.

**Why:** "postgraduateresearch" is the Drupal/generic UK university pattern; Strath uses a shorter "research" segment.

## H1 span structure (all page types)

All three Strath page types (UG, PGT, research) wrap the course title in:
```html
<h1>
  <span class="superscript">BSc Hons</span>
  <span class="course-title">Computer Science</span>
</h1>
```

- `//h1/text()` returns **nav H1 text** (e.g. "Professional services") — wrong.
- `//h1/span[@class='course-title']/text()` is precise across all types.
- `//h1/span[@class='superscript']/text()` gives the degree type (e.g. "MPhil, PhD") — use as degree fallback for research pages which have no graduation-icon div.

## How to apply
When setting block_url_patterns for any uni: if a path segment like `/research/` also appears under `/courses/`, scope the block to the hostname (`"strath.ac.uk/research/"`) not just the segment (`"/research/"`). Always verify the real PGR URL path before writing the YAML — it differs by CMS (Drupal, T4, etc.).
