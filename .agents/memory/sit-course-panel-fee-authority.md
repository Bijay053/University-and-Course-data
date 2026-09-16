---
name: SIT course panel and fee authority
description: Southern Institute of Technology’s safe extraction boundary and international tuition semantics.
---

SIT course pages wrap a small current-programme summary in a very large shared site shell. Treat the current campus, key-information pairs, semester start dates, and application criteria as course-owned; exclude navigation, contact forms, related programmes, module content, and local fee prose.

**Why:** Repeatedly parsing the full page exceeded the per-course deadline, while shared contact text turned “0800 4 0 FEES” into a location and module levels corrupted duration. Semester end dates also looked like intakes.

**How to apply:** Compact before generic extraction. Emit explicit labelled facts and only semester start months. If the expected component changes, retain bounded title evidence rather than restoring the full shell.

SIT’s central international schedule is the tuition authority. Use its Tuition Fee column, not Resource Fee or Total Fee, force NZD, and require exact award-name matching.

**Why:** Course pages advertise domestic Zero Fees and direct material costs. The schedule’s total combines tuition and resource costs, while fuzzy matching can map certificates to diplomas or unrelated awards.

**How to apply:** Central tuition may override local amounts only after an exact programme match. A course absent from the populated international schedule is not eligible for staging; a schedule fetch failure remains retryable.