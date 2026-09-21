---
name: Settings-backed academic requirements
description: Why course academic prerequisites reference configurable options through stable semantic keys.
---

Course academic prerequisites derived from degree levels must reference the existing Academic Level option by ID. The degree mapping targets a stable semantic key, while every displayed name comes from the current option record.

**Why:** Administrators can rename Academic Level settings. Copying a name into each course would leave stale values and make future scrapes reintroduce hardcoded labels.

**How to apply:** For mapped degree levels, fail closed if the semantic option is unavailable. Preserve option IDs through staging, snapshots, edits, and approvals; use legacy text only as a read fallback for historical unmapped rows.