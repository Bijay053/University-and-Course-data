---
name: Autonomous URL repair safety
description: Safety contract for operator-triggered OpenAI URL-filter repairs.
---

OpenAI may diagnose and propose discovery changes, but deterministic code must
validate the complete effective allow, block, must-contain, and final
course-detail filter before any write. A total-loss job must rescue a material
share of known course URLs, not merely one sample.

**Why:** Partial simulations and heuristic regex suggestions can look successful
while another active gate still drops every course. Model output is evidence,
not authority to mutate a scraper recipe.

**How to apply:** Reject malformed or non-improving proposals. Persist YAML and
database state atomically, reload the merged config, and roll both back on any
mismatch. Use a fenced per-university lease token and verify ownership at the
write boundary so concurrent or expired repair runs cannot overwrite each other.
Keep extraction-field proposals advisory until they have a non-mutating,
field-specific validation path.