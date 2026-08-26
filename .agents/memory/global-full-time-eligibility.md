---
name: Global full-time eligibility
description: International catalogue policy for mixed, equivalent, part-time-only, and unconfirmed study-load wording.
---

The international catalogue accepts only courses with a confirmed full-time route. Mixed wording such as “full-time or part-time equivalent” is stored as Full Time, and a value of Both is normalized to Full Time. Explicit wording such as “only available part-time” overrides an “equivalent full-time study” workload measure and is ineligible. A final unresolved Part Time value fails closed.

**Why:** Checking for “part-time” first misclassified full-time courses across many universities, while checking for “full-time” first alone admitted courses that quote a full-time-equivalent workload but are actually offered only part-time.

**How to apply:** Prefer the DOM value paired with the Duration label. Apply explicit part-time-only phrases first, then full-time/mixed availability, then implied primary full-time wording such as “N years, or part-time equivalent.” Quarantine unsupported live rows rather than deleting them.