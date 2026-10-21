---
name: Central profiles in staged re-extraction
description: Durable rules for using and invalidating central English profiles during Course Checking fixes.
---

Review → Fix must prefetch the university's configured central pages and pass that payload into per-course extraction, just as a normal scrape does. Any semantic change to central English parsing must also increment the English cache schema version.

**Why:** A Murdoch course-code parser worked in fresh live-page tests, but Course Checking fixes kept the generic level defaults because re-extraction omitted central data and production accepted an older parsed cache as current.

**How to apply:** When changing central-page parsing or staged re-extraction, verify the recovery call receives `central_data`, verify a previously populated row can be overridden, and use an old-version cache fixture to prove automatic reparsing.

Murdoch accordion categories may contain multiple independently named tables,
blank formatting-only `<strong>` nodes, bare program-name `<strong>` elements
followed by code text, and dated old/new tables for the same course code.

**Why:** Reading only the first table or nearest `<strong>` silently dropped
B1422 and Teaching profiles; first-match precedence also retained an obsolete
Nursing test table.

**How to apply:** Bind every table to the nearest non-table course identity,
preserve all codes/names in a shared heading, prefer the later exact-code table
when codes repeat, and bump the English cache schema for any semantic change.

Explicit undergraduate/postgraduate table columns must retain their level
boundaries, including other English tests and named-course exceptions.

**Why:** Liverpool Hope places both level headings before the IELTS row.
Flattening the page assigned the undergraduate score to postgraduate study.
Separate qualification-equivalence tables also repeat IELTS figures that are
not replacements for the primary level requirements.

**How to apply:** Parse the primary test rows by their owned column, keep
named exceptions scoped to their awards, and never store a new-scale TOEFL
score as if it used the legacy 0–120 scale.