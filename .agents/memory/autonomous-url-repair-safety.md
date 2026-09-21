---
name: Autonomous URL repair safety
description: Safety contract for operator-triggered OpenAI URL-filter repairs.
---

OpenAI may diagnose and propose discovery changes, but deterministic code must
validate the complete effective allow, block, must-contain, and final
course-detail filter before any write. A total-loss job must rescue a material
share of known course URLs, not merely one sample.

Repair evidence must itself pass the production course-page classifier.
Low-depth jobs often contain only menu, fees, faculty, or category links; probe
the sitemap outside the broken allowlist to obtain real course candidates.
After a validated repair is saved, run a fresh scrape and require staged courses
before presenting the university as recovered.

**Why:** Partial simulations and heuristic regex suggestions can look successful
while another active gate still drops every course. Navigation links can also
produce a misleading 100%-rescued simulation yet stage zero courses. Model
output is evidence, not authority to mutate a scraper recipe.

**How to apply:** Reject malformed or non-improving proposals. Persist YAML and
database state atomically, reload the merged config, and roll both back on any
mismatch. Use a fenced per-university lease token and verify ownership at the
write boundary so concurrent or expired repair runs cannot overwrite each other.
Keep extraction-field proposals advisory until they have a non-mutating,
field-specific validation path. Treat a current non-empty simulation as
authoritative over stale job counters, and fail closed when the verification
scrape stages nothing.

Large valid course pages must be truncated to the bounded evidence budget, not
rejected solely because the full HTML exceeds that budget. A degree-qualified
course title plus multiple course-owned facts remains positive evidence even
when foundation-year or status links make a generic page classifier say
“listing.”

**Why:** Canterbury course pages are roughly 2–2.5 MB, but their title, study
mode, duration, and location appear within the first 60 KB. Rejecting the whole
response at 1 MB blocked every automatic repair probe.

**How to apply:** Preserve the first bounded byte window with UTF-8-safe
truncation, then run the ordinary challenge, ownership, field, and eligibility
checks. Never increase or remove the evidence cap merely to accept a large page.