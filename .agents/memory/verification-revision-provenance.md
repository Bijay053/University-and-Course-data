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