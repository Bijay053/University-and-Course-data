---
name: Verification continuation safety
description: Why verification continuation is limited to acknowledged timeouts and exact remaining samples.
---

Continue verification only after cancellation has completed and the child has recorded a terminal time-budget result. Use the persisted canonical sample identity, not a fresh catalogue or the displayed course website.

**Why:** A verification timeout can leave useful staged courses, but reclaiming an active worker risks duplicate writes, and rediscovering URLs changes the sample being certified. Older runs without persisted selected URLs cannot be safely continued retroactively.

**How to apply:** Preserve staged siblings, bound cumulative runs/time/observed provider cost, and evaluate safety across every child. A clean continuation must not erase earlier errors, contamination, critical-quality failures, or skipped courses. Every selected URL must have a conserved terminal outcome across staged, explicitly classified skip, or error evidence; one unaccounted URL must keep the run in review even when the remaining sample is clean. Keep publishing manual and full-catalogue coverage uncertified.

Course-report recovery is a separate, explicitly reviewed continuation policy: each new bounded run requires human acknowledgement rather than consuming the autonomous verification continuation allowance.

**Why:** Large reported sets must be recoverable beyond one run without silently authorizing more work or confusing attempted coverage with successful recovery. An exhausted automatic-verification allowance must not redefine a reviewer's separate authorization.

**How to apply:** Retain original report identity and exact unattempted URLs across runs; count settled skips/errors as attempted, not recovered. Keep each run's limits and prior review rows, and never infer full catalogue coverage from completion of the known URL set.

Checkpoint settled outcomes before emitting progress or starting other cancellable work, including discovery/prefetch exclusions.

**Why:** Batch-end checkpoints miss already-settled skips and errors when a deadline interrupts event emission. Shielding a commit alone is insufficient if timeout rollback can run before that commit finishes.

**How to apply:** Wait for in-flight checkpoint persistence before propagating cancellation; test cancellation at the outcome boundary and during persistence, not merely statement ordering.

Cross-worker acceptance must prove distinct worker identities; queue delivery alone is not evidence of a process handoff.

**Why:** A persistent worker can execute both children and hide serialization, stale resource, or process-boundary defects.

**How to apply:** Stop the first worker after its durable terminal checkpoint, start a replacement, and assert different recorded worker identities plus exact remaining work.
