---
name: Requirement verification display
description: Keep extracted IELTS values visible without treating them as verified.
---

An unknown English verification state must not replace an available IELTS score in review. Show the extracted score and the verification warning together.

**Why:** Reviewers interpreted the replacement “Unverified” label as failed extraction even when an overall score was stored.

**How to apply:** Keep verification, missing-component warnings, and source evidence separate from value visibility. Never change eligibility or verification merely to display a score, and never invent a score when none is stored.