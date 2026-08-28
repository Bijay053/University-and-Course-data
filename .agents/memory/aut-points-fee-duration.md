---
name: AUT points-based fees and duration
description: How AUT programme points govern annual fee and duration normalization when page metadata conflicts.
---

For AUT, treat 120 programme points as one full-time year. The International fee card’s first currency amount is the levy-inclusive total for the stated points; normalize it to an annual fee with `amount × 120 ÷ points`. Derive duration as `points ÷ 120` years when duration metadata conflicts.

**Why:** AUT can publish 180 points beside stale “1 year” duration metadata. Fee-card prose may also begin with a year such as “not offered in 2027,” and the breakdown then labels a smaller tuition-only subtotal. A broad amount matcher selected either the year or subtotal instead of the headline total.

**How to apply:** Match the first explicitly currency-prefixed amount in the International card, read its adjacent “for N points” qualifier, retain the student-services levy, and use points as the workload authority for both annual fee and full-time duration.