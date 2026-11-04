---
name: Requirement verification display
description: Keep extracted IELTS values visible without treating them as verified.
---

Name the unresolved requirement in the Score column, and explicitly label source-verified IELTS in its own column.

**Why:** Users saw an unchanged generic “Unverified” score after IELTS verification succeeded because academic requirements remained unresolved. Re-running IELTS extraction could never resolve that warning.

**How to apply:** Keep academic missing/unverified states separate from English verification in both the visible review and assistant context. Do not diagnose a stale UI or rerun English extraction solely because the academic requirement remains unresolved.

An unknown English verification state must not replace an available IELTS score in review. Show the extracted score and the verification warning together.

**Why:** Reviewers interpreted the replacement “Unverified” label as failed extraction even when an overall score was stored.

**How to apply:** Keep verification, missing-component warnings, and source evidence separate from value visibility. Never change eligibility or verification merely to display a score, and never invent a score when none is stored.