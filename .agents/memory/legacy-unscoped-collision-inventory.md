---
name: Legacy unscoped collision inventory
description: Reconciliation previews based on scoped evidence can miss older published courses that still collide with the approved award.
---

A campus-split evidence snapshot is not a complete inventory of published award
identities. Before a historical ID reconciliation, query **all** published
courses for the university by source route and award, including IDs whose
staged evidence lacks campus-split metadata. Treat matches outside the exact
reviewed ID map as blockers until their identity is reviewed.

**Why:** A scoped-evidence preview passed with every approved group eligible,
but the live read-only apply check found older same-award, same-route courses
with matching mode and fee cohort. Those records were outside the snapshot and
could not be assumed to be distinct or silently aliased.

**How to apply:** Compare the reviewed map to the complete live course inventory
before attestation or write. Preserve all original IDs and source evidence;
obtain explicit approval for any expanded alias membership and revalidate its
fee and study identity independently.