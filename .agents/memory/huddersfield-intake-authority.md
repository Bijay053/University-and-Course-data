---
name: Huddersfield intake authority
description: SearchStax start-date placeholders and concatenated course facts.
---

Huddersfield's Solr metadata can say “Multiple start dates” while its indexed
course content contains the exact selected-year dates. Do not infer months
from the broad start-date range facets.

**Why:** Live records expose precise dates in a `Start Dates` fact immediately
after the year selector, with no whitespace between the preceding year and
the label or between the last date and `Duration`. Word boundaries at the
label therefore fail. Page-wide month scanning also captures deadlines.

**How to apply:** Preserve usable structured dates; otherwise consume only
the labelled contiguous full-date list and retain that exact source evidence.
Keep the course URL/year paired with its own facts.