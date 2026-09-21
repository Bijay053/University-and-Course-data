# Autonomous worker ownership rollout

This change requires the additive Alembic revision `383_claim_lock_lineage`
(including its predecessor `382_worker_claims`).
It protects automatic repair and autonomous verification. It does not enable
automatic course publishing.

## Deployment prerequisites

1. Use the existing guarded release and idle checks. Do not replace old workers
   while they are executing work: older code does not enforce generation checks.
2. Inspect the actual target database and migration history before authorizing
   any schema change. The read-only prerequisite checker is
   `PYTHONPATH=. python -B -m app.services.worker_fencing_schema`.
   The guarded release streams this checker from the exact reviewed target
   before checkout, smoke tests, consumer cancellation, or restarts.
   It verifies the claims/budgets columns, including revoked-generation lineage;
   it does not treat an Alembic version label as proof that the schema exists.
3. Approve and apply only the reviewed prerequisite schema change before
   starting new workers. When the actual predecessor is verified as
   `381_acad_req_option`, the ordinary two-revision migration path may be used.
   If the recorded revision is older (production was observed at
   `341_alert_delivery`), do **not** run a generic upgrade to 383: that would
   also execute unreviewed intervening migrations. Instead inventory existing
   objects and rehearse a separately reviewed, transactional, schema-only
   installation of the exact 382/383 objects: `autonomous_worker_claims`,
   its `(task_id, process_identity)` index and state constraint,
   `autonomous_worker_budgets` with its claims foreign key, and
   `revoked_generations JSONB NOT NULL DEFAULT '[]'::jsonb`.
   Verify existing object types/constraints before considering any
   `IF NOT EXISTS` operation; never overwrite or clear existing claim rows.
   Record the scoped installation independently and leave Alembic's historical
   marker unchanged until the migration lineage is separately reconciled.
   Use the deployment's configured database identity, obtain a backup and
   rehearse rollback/reapplication on an isolated copy first. These instructions
   describe required authorization; they do not authorize production writes.
4. Rerun the read-only check and guarded idle checks, then replace the API and
   all Celery workers together using the exact reviewed release.
5. Verify API startup, Celery startup, and the configured release identity.
   An application startup alone does not apply this migration.
   Preserve the failed repair audit. Start a newly authorized bounded repair
   and verify its durable claim and result; do not reset the old failed session,
   raise retry budgets, or claim a live fix merely because the schema check passes.

These migrations create ownership and persisted live-fetch-budget tables and
add revoked-generation lineage for safe Redis lock adoption. They do not rewrite
courses or existing scrape results. Verify the migration target in each
environment; production requires its normal authorized migration/release process.

## Recovery boundaries

- Only process exit confirmed through Linux pidfd, or a completed stop
  acknowledgement, authorizes revocation.
- Heartbeat age, missing inspection replies, worker startup, task-result
  exception names, and a revoke request do not authorize recovery.
- A task intentionally stopped by an operator is not automatically resumed.
- Existing legacy claims are not automatically reclaimed.
- Missing pidfd support, whole-parent/host death, and failed proof persistence
  leave ownership fenced for investigation.
- Verification replacements retain their deterministic job identity, staged
  rows, selected sample, and remaining budgets. They do not certify the whole
  catalogue.

Do not manually clear claim rows or edit generations to unblock a scrape.
That bypasses the write fence and can reintroduce conflicting workers.

## Verification

`tests/test_worker_fencing.py` covers disposable PostgreSQL races and isolated
Redis/Celery prefork lifecycles, including hard-timeout exit and repeated
deliveries. These use synthetic tasks, not live provider/S3 workflows.
Run with the project's usual pytest environment. No production fault injection
is required or authorized by this document.