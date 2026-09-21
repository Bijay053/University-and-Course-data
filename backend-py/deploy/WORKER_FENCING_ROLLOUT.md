# Autonomous worker ownership rollout

This change requires the additive Alembic revision `383_claim_lock_lineage`
(including its predecessor `382_worker_claims`).
It protects automatic repair and autonomous verification. It does not enable
automatic course publishing.

## Deployment prerequisites

1. Use the existing guarded release and idle checks. Do not replace old workers
   while they are executing work: older code does not enforce generation checks.
2. Apply the migration before starting the new workers:
   `PYTHONPATH=. python -m alembic upgrade 383_claim_lock_lineage`
   Use the deployment's configured interpreter and database environment.
   Do not substitute development credentials or blindly stamp a revision.
3. Replace the API and all Celery workers together once the idle gate passes.
4. Verify API startup, Celery startup, and the configured release identity.
   An application startup alone does not apply this migration.

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