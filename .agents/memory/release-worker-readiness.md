---
name: Release worker readiness
description: Distinguishing startup readiness from task-lifecycle safety during production releases.
---

Use a bounded worker-readiness phase before strict idle inspection after restart.
Do not interpret an initial lack of ping replies as evidence of business work,
and do not omit the eventual complete idle inspection.

**Why:** Production services can be active before the Celery control channel
responds. A release can update the backend yet restore the previous frontend
when the immediate post-restart inspection fails.

**How to apply:** Wait finitely for complete worker replies, then enforce the
ordinary business-work and unknown-task refusal rules. Release samples can also
enqueue performance-recording follow-ups after returning; account for their
verified sample ownership and wait for their real completion rather than
globally ignoring that task type or claiming a fully completed release.
