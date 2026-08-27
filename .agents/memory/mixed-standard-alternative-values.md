---
name: Mixed standard and alternative values
description: Guardrails for extracting standard course values from prose that also contains accelerated durations or named alternative English tests.
---

Do not reject an entire sentence merely because it contains an accelerated duration when the same sentence also publishes the standard duration. Remove only the nearest numeric duration attached to the accelerated marker, then parse the surviving standard value. If no distinct standard duration remains, continue to fail closed.

**Why:** Course pages can publish “5 years … 4 years accelerated” in one field. Whole-sentence rejection loses the authoritative standard duration, while a broad removal can accidentally consume an earlier fee period or the standard value.

**How to apply:** Temper alternative-duration matching so it cannot cross another duration token. For named English-test regexes, require numeric boundaries on both sides of the captured score so a year such as “2009 onwards” cannot be truncated into a plausible score.