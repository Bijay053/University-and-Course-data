---
name: Pending import reuse
description: Identity-safe repeated imports retain pending review ownership and separate attempt evidence.
---
Repeated imports must keep the existing pending row and staff values rather than replacing or reparenting them. A differing extraction is evidence to review, not permission to overwrite. Distinct awards, campus offerings, fee periods, years and study variants remain separate.

**Why:** Fetching the same course again is not a new reviewer decision; deleting the old review loses staff edits, while copying it creates duplicate work.

**How to apply:** Match the immutable initial offering identity, not current staff-editable identifying values. Link matching pending rows into the requesting review and append the incoming attempt separately. Keep reused rows out of subsequent automatic verification, quality backfills and repair/publication writes, including same-job retries. Continue metrics, alerts and scoped processing of newly staged rows in mixed jobs. Attempt evidence is not a staged-row restore backup.

Legacy rows need their original staged snapshot associated by provider, owning job, fee scope and row-owned source evidence. Do not infer identity from a corrected name or pick one of several ambiguous matches.

**Why:** Existing reviews predate import-attempt identities; matching only current fields recreates duplicates after staff corrections.
