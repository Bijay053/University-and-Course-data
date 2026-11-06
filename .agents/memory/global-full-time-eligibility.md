---
name: Global full-time eligibility
description: International catalogue policy for mixed, equivalent, part-time-only, and unconfirmed study-load wording.
---

The user changed the staging policy on 2026-11-05: retain part-time-only courses in pending review with visa-eligibility warnings; never automatically publish them. A confirmed full-time route is still necessary before treating a course as full-time eligible. Mixed wording such as “full-time or part-time equivalent” is stored as Full Time, and Both normalizes to Full Time. Explicit “only available part-time” overrides a full-time-equivalent workload measure.

**Why:** Checking for “part-time” first misclassified full-time courses across many universities, while checking for “full-time” first alone admitted courses that quote a full-time-equivalent workload but are actually offered only part-time.

**Policy change reason:** The user explicitly chose “Keep them flagged for review” when resolving the updated scenario document against the old exclusion rule.

**How to apply:** Prefer the DOM value paired with the Duration label. Apply explicit part-time-only phrases first, then full-time/mixed availability, then implied primary full-time wording such as “N years, or part-time equivalent.” Quarantine unsupported live rows rather than deleting them.

UTAS duration panels append catalogue-wide explanatory prose saying that study time depends on full/part-time choice and that “some programs are only available part time.” This is not evidence about the current course. Strip that whole boilerplate before classifying; an explicit current-course mixed/full-time statement remains authoritative.

**Why:** Treating the shared sentence as course-specific rejected hundreds of valid UTAS courses before extraction, including courses whose same panel explicitly offered both part-time and full-time study.

**How to apply:** Generic “some programs” guidance can never prove part-time-only status. Require wording anchored to the current course or a bounded duration value that offers only part-time study.

Legacy staged rows can hold “Part-time” in study mode while study load is missing or defaulted to “Full Time.”

**Why:** Approval must not treat a defaulted load as verified availability when the course-specific mode explicitly says part-time.

**How to apply:** Check both fields at the final publication boundary, including force and bulk actions. Mixed full-time/part-time availability remains eligible; unknown mode alone is not proof of part-time-only delivery.