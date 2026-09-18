---
name: Autonomous config repair rollback
description: Safety boundary for agents that validate and persist scraper configuration changes.
---

Autonomous scraper repairs must validate against immutable evidence without mutating staged or production rows. Persist only after field-specific improvement and preservation checks pass.

**Why:** A compare-and-swap apply is not enough. If later processing fails, an unconditional compensating restore can erase a newer operator edit made after the repair was saved.

**How to apply:** Fence the initial write against the exact pre-validation config and fence rollback against the exact config written by that repair. If either comparison fails, preserve the newer config and report the conflict instead of overwriting it.

Automatic verification must use isolated, freshly extracted review rows, not ordinary resume behaviour or inherited published values. Configuration acceptance and successful live verification are separate outcomes; a capped sample cannot certify catalogue completeness.

**Why:** Normal reruns can replace pending review work, and checkpoint reuse or inherited values can make an ineffective repair appear successful. The user approved automatic testing, application, and bounded reruns, not automatic publication or removal of existing courses.

**How to apply:** Preserve the original review set, compare real verification results, disclose sample/time limits, and retain manual review when source access, coverage, or field authority is unresolved.

The durable repair schema is a startup prerequisite, not an optional audit enhancement. API startup and every worker entry path must idempotently verify it before accepting or reconciling repair work.

**Why:** A release once included the model and migration but the target database had not applied the migration. Repair tasks stayed “queued” while the periodic reconciler failed on the missing table.

**How to apply:** Keep the numbered migration as the deployment source of truth, plus a safe `CREATE IF NOT EXISTS` rollout guard on API startup and worker repair/recovery entry points.
