---
name: London Met audience-scoped entry points
description: London Met course entry points separate UK and Overseas cohorts in one selector.
---

London Met international intakes must come from the Overseas/International options inside the course entry-point selector. Select the earliest eligible current/future dated cohort and keep its fee, location, duration, and months together.

**Why:** Page-wide scanning mixed UK and Overseas values, mixed later-year months into the selected cohort, and could reuse past-only fees. The official undergraduate English score is on a linked requirements page, not reliably numeric on each course page.

London Met explicitly includes foundation-year degrees under its published undergraduate English requirements. Do not treat a BA/BSc/BEng **including** a foundation year as a standalone preparatory pathway merely because an early degree classifier returns Foundation.

**Why:** Its central requirements page was successfully fetched, but pathway classification prevented the verified requirements reaching integrated degrees. A separate audience-identity guard also erased ordinary institution-wide central data when neither side had an audience identity.

**How to apply:** Preserve institution-wide central English data when neither payload is audience-scoped; require matching identities only for genuinely audience-scoped data. Verify a final extracted payload, not just a successful central-page fetch.

Do not invent a numeric academic score to make postgraduate completeness reach 100%. London Met commonly publishes classifications such as a 2:2 honours degree rather than UCAS points.

**Why:** A degree classification is not a numeric UCAS score or percentage. Preserve the classification in the entry-requirement text; only explicitly labelled UCAS values belong in numeric UCAS fields.

**How to apply:** Require audience identity from the same option/optgroup, exclude unrelated international markup, reject past-only dated cohorts, and use yearless options only when no eligible dated cohort exists. Resolve undergraduate IELTS from the bounded official standard-requirements section while excluding later exception/academic-IELTS sections.