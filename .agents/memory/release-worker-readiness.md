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

Use the reviewed target's lifecycle rules for the pre-checkout sample, without
changing the installed application's source first.

**Why:** Otherwise the installed release's faulty sample rules can prevent
deploying the tested correction to those same rules. A relocated helper also
needs its original package context to resolve pinned proof dependencies.

**How to apply:** Preserve revision fencing and proof validation, materialize
the immutable target helper temporarily beside its proof assets, preserve its
package context in observer subprocesses, and remove it before checkout.

A consumer pause is not durable across worker restart. Recheck actual queue
bindings and business work afterward rather than relying on a prior idle
observation.

**Why:** A legitimate scrape began during post-restart readiness despite the
pre-restart pause and complete idle checks. The release correctly refused it
and restored the previous frontend.

**How to apply:** Keep the unrelated-business-work refusal. Let existing work
and its scheduled follow-ups finish naturally before an exact-revision guarded
retry; never revoke or reclassify it as maintenance to finish a release.
