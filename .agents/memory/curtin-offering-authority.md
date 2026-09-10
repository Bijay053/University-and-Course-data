---
name: Curtin structured offering authority
description: Source boundaries for Curtin duration, delivery, location, and international fees.
---

Curtin course duration, attendance mode, and location must come from the current offering’s labelled key-information blocks. Mixed durations such as years plus months are one value and must be converted without dropping the month component.

**Why:** Flattened page text includes tooltip, related-major, and credit-point content. It produced 66-year durations and broad Perth/On Campus defaults.

**How to apply:** Scope each value to its own information block. Preserve an explicit blank when a course-owned block is absent so page-wide text cannot refill it.

Curtin international annual tuition comes from the newest “International – Indicative year 1 fee” Offer in the current page’s JSON-LD, not the visible domestic fee panel.

**Why:** The international query can still render domestic fee UI while the structured course data carries both domestic and international offers.

**How to apply:** Select only AUD international year-1 offers, retain the stated fee year, and keep the amount, currency, Annual period, and evidence atomic. Ignore total-course and domestic offers.