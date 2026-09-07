---
name: CQU English field drift
description: How to preserve course-specific CQU English requirements when current templates move them between structured fields.
---

Treat the current course's AIMS data as authoritative, but inspect course-scoped requisite and entry-requirement text when the dedicated English field and Course schema prerequisites are empty.

**Why:** CQU can publish the complete visible English section inside requisite conditions while leaving both the dedicated English field and schema prerequisites empty. Falling through to institutional defaults then replaces lower, course-specific scores with generic values.

**How to apply:** Accept alternate AIMS fields only when they belong to the current course object and explicitly mention English tests. For TOEFL clauses listing paper-based and internet-based scores together, skip out-of-range PBT values and retain the valid iBT overall score.