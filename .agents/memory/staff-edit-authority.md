---
name: Staff edit authority
description: Explicit staff values are a separate authority from official source verification.
---
Use staff-provided provenance for literal values supplied in chat, without fetching pages to pretend those values are independently verified. Keep contradictory selected official evidence as a review blocker.

**Why:** Staff corrections and source-backed recovery are different requests. Requiring a page to repeat a staff value prevents legitimate corrections; labelling it verified invents evidence.

**How to apply:** Preserve atomic amount/unit bundles, provider scoping and confirmation boundaries. A repeat applies the most recent edit, including when its field differs from earlier messages. An unsupported intervening edit must block reuse of older values. Related published records belong in stale-preview fences, not just the parent course timestamp.

Bare numbered rows refer to pending review records; published IDs require an explicit published selector.

**Why:** Pending and published IDs are independent namespaces and can collide within one provider.

**How to apply:** Apply record-kind filtering before numeric matching. Month-only edits must not reopen closed intakes or discard dates.

Do not reuse research-wide targeting for writes.

**Why:** Research helpers can interpret “others” as broad scope, including inside “leave others unchanged.”

**How to apply:** Resolve explicit row IDs before names and URLs; ambiguous provider-wide exclusions must fail closed.

Undo must detect intervening writes even when the visible value returns to its original value, and must preserve null versus absent provenance.

**Why:** Content-only fingerprints miss edit-away/edit-back sequences; treating null provenance as an empty map is not an exact restoration.

**How to apply:** Fence location undo with both persisted row content and PostgreSQL tuple revision. Treat missing legacy undo fences as unsafe rather than guessing. Restore only the original edit's evidence; do not supersede unrelated evidence.

Compound staff instructions retain separate field scopes: “all missing course
intake” fills empty intakes, while “all course location” changes locations
without replacing existing intakes.

**Why:** The user reported a combined intake/location instruction being treated
as an ambiguous single-course edit, followed by unwanted course-name matching.

**How to apply:** Prepare one combined preview with per-row field changes.
Apply confirmation limits to the complete affected row set, not each clause
individually. Explicit follow-ups retain the original fields and scope.

The bulk-update bug brief requires its natural-language request to affect only
pending records in the current review. “Add Sydney to all course locations”
retains other locations and does not duplicate Sydney.

**Why:** The user explicitly specified current-review scope and additive,
duplicate-free location updates in the supplied bug report.

**How to apply:** Do not silently include other reviews or published courses.
Keep provider-wide edits as a separate, explicitly broader instruction.
