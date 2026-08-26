---
name: UOW IELTS skill table
description: Why UOW course-level IELTS overall scores extracted while explicit Reading, Writing, Listening, and Speaking scores did not.
---

UOW course pages can publish an explicit table whose columns are `Overall Score`, `Reading`, `Writing`, `Listening`, and `Speaking`, followed by an `IELTS Academic` data row. Parse these tables by DOM header index rather than relying on flattened text order.

**Why:** Flattened text places every band label before the `IELTS Academic` token. The prose parser therefore found the overall score but could not associate any subsequent numbers with the preceding labels, leaving all four saved sub-band fields blank.

**How to apply:** Prefer a high-confidence table result when an IELTS row has an overall and at least two recognized skill columns. Map each score by its normalized column header so reordered columns remain correct.