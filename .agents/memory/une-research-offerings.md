---
name: UNE research offerings
description: How to interpret UNE research availability tables and institutional English defaults without inventing intake months.
---

UNE research-course availability tables are authoritative by physical-campus row: explicit “Not Offered” cells must exclude that campus. Responsive markup may duplicate both header labels and tooltip text, so table recognition must tolerate repeated period labels and a longer “Start Dates and Campus” heading.

**Why:** UNE’s live research pages can list Online and Armidale as Offered while Sydney is Not Offered. Falling through to generic page text incorrectly selects Sydney from unrelated content.

**How to apply:** Parse Research Period columns as an availability pivot, ignore Online when deriving physical location, and retain the published labels “Research Period 1” and “Research Period 2” as intakes. UNE does not publish a reliable month mapping on the course or embedded page data, so do not translate these labels into guessed months.

UNE institutional English defaults are fill-only and research-aware: general courses use IELTS 6.0, PTE 57, TOEFL 79; Higher Degrees by Research use IELTS 6.5, PTE 64, TOEFL 91. A research Master must select the research profile from course context rather than the generic postgraduate tier.

**Why:** UNE course pages link to central policy but often contain no numeric scores, while official policy distinguishes research degrees from the general university minimum.

**How to apply:** Preserve stronger course-specific extracted evidence. Only fill missing slots from the appropriate institutional profile, and keep duplicate UNE recipe files aligned.