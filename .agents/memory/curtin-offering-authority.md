---
name: Curtin structured offering authority
description: Source boundaries for Curtin duration, delivery, location, and international fees.
---

Curtin course duration, attendance mode, and location must come from the current offering’s labelled key-information blocks. Mixed durations such as years plus months are one value and must be converted without dropping the month component.

**Why:** Flattened page text includes tooltip, related-major, and credit-point content. It produced 66-year durations and broad Perth/On Campus defaults.

**How to apply:** Scope each value to its own information block. Preserve an explicit blank when a course-owned block is absent so page-wide text cannot refill it.

Curtin international annual tuition comes from the newest exact international
year-one fee in the current offering. Prefer JSON-LD Offers; postgraduate
pages may instead expose equivalent structured
`.fees__international .fee[data-segment=int][data-fee-key=YR1_IND_INT]`
cards.

**Why:** The international query can still render domestic fee UI. Direct
responses often carry both audiences in JSON-LD, but static-proxy recovery
returns postgraduate international fee cards only when Curtin’s
`user_region=int` cookie is forwarded.

**How to apply:** Forward the audience cookie through every transport. Select
only AUD international year-one Offers or exact `YR1_IND_INT` cards, retain the
stated fee year, and keep amount, currency, Annual period, and evidence atomic.
Ignore total-course and domestic values.

Intake values must be calendar months, never Rolling or Research Term labels.
Unknown research intake months remain empty for review.

**Why:** Converting Research Term 1/2 to Rolling falsely marked intake complete
and displayed an invalid “Rol” value. The user explicitly requires months only.

**How to apply:** Preserve verified calendar month evidence; do not translate
year-round admission into arbitrary months or infer dates from deadlines.

Treat prevention and historical repair as separate deliverables.
**Why:** An undeployed parser correction left production review rows displaying
“Rol”; fixing future extraction alone does not clear stored invalid values.
**How to apply:** Verify the serving release and repair affected pending rows
with before-images, refreshed review scores, and invalidated stale evidence.
Do not interrupt unrelated active scrapes just to restart corrected workers.