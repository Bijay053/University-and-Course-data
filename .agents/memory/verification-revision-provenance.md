---
name: Verification revision provenance
description: Keep live-run evidence and post-integration test claims tied to the code they actually verified.
---

Record the exact revision for live verification and distinguish pre-integration
evidence from checks on the final integrated tree.

**Why:** Completion can rebase task commits onto concurrent main-branch work.
A passing targeted test and full live scrape can therefore precede a different
runtime tree at completion; even an adjacent helper definition can be lost
during integration.

**How to apply:** When completion validation contradicts an earlier pass,
compare the current tree and revision before diagnosing the original change
again. Repair the integrated code, rerun affected checks, and preserve the
original evidence's revision rather than relabelling it as proof of later code.

Reconcile a reported release against its exact successful command target and
required commit ancestry, not just the fact that a deployment completed.

**Why:** A successful release preceded the lifecycle change it was later
assumed to contain. Later releases on the same host resolved the missing code,
but could not make the earlier acceptance observations successful.

**How to apply:** Preserve the historical observation, compare the receipt's
target with the required change, and independently check current checkout,
process identities, and deployed source. A failed post-checkout release can
leave new backend code running; do not equate checkout movement with completion
or assume that a failed release fully rolled back.

For production course-identity reconciliation, treat a reviewed ID mapping as
approval for that exact membership, not as permission to absorb newly
published IDs. New scrape runs can connect previously separate groups even
when every old ID still exists.

**Why:** A live pre-write inventory found two new published rows bridging four
previously approved groups. Reusing the old approval would silently change
which historical IDs resolve to the same public course.

**How to apply:** Refresh connected components immediately before the write;
if membership changed, pause the write and obtain approval for the revised
mapping. Preserve all old IDs and never infer approval from matching titles.