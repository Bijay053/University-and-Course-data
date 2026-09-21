---
name: Verification continuation safety
description: Why verification continuation is limited to acknowledged timeouts and exact remaining samples.
---

Continue verification only after cancellation has completed and the child has recorded a terminal time-budget result. Use the persisted canonical sample identity, not a fresh catalogue or the displayed course website.

**Why:** A verification timeout can leave useful staged courses, but reclaiming an active worker risks duplicate writes, and rediscovering URLs changes the sample being certified. Older runs without persisted selected URLs cannot be safely continued retroactively.

**How to apply:** Preserve staged siblings, bound cumulative runs/time/observed provider cost, and evaluate safety across every child. A clean continuation must not erase earlier errors, contamination, critical-quality failures, or skipped courses. Keep publishing manual and full-catalogue coverage uncertified.