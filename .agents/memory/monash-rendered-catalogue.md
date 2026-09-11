---
name: Monash rendered catalogue source
description: Live constraints for Monash Find a course discovery and detail extraction.
---

Monash's rendered `https://www.monash.edu/study/courses/find-a-course` page
contains the broad catalogue in one DOM. Undergraduate, graduate,
international, and full-time listing query parameters were preserved by the
browser but produced the same 565 URL-shaped records, so they are not safe
discovery filters.

Filter before detail fetch. In the September 2026 bounded production render,
PDM/PDD suffixes identified 112 professional-development pages. Monash-local
certificate/professional title rules removed another 9 while retaining degree
codes such as B2029 and B6038, leaving 444 candidates. Do not add
`professional certificate` to the fleet-wide non-degree regex.

In browser discovery, an explicit per-university course allow-pattern must run
before the generic course/category classifier.

**Why:** The generic classifier treated valid Monash degree URLs as navigation,
so 400+ links visible in the rendered catalogue never reached the correct
Monash allowlist and discovery collapsed to zero.

**How to apply:** Apply block rules first, then accept exact allow-pattern
matches as course candidates. Use the generic classifier only when no explicit
allowlist exists.

Appending `international=true` to B2029 preserved the parameter and exposed
international entry requirements plus `On-campus at Caulfield: Full time`, but
only linked to generic fee information. Treat the detail view as authority for
international/location/load filtering. Leave absent tuition blank for review;
never infer a fee from the listing or generic navigation.