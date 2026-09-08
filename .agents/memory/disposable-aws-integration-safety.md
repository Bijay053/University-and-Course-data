---
name: Disposable AWS integration safety
description: Safety boundary for integration tests that use separate AWS principals or mutate remote resources.
---

Destructive AWS integration tests must validate the account identity of every principal they use and must verify an explicit disposable-resource tag before mutating a remote host or service.

Infrastructure rehearsals must exercise the real queue/worker semantics and validate runner-to-template resource identifiers; a parked message or source-string assertion is not operational proof.

**Why:** S3 and SSM operations can use different credential sources. Validating only the storage principal does not prove that the SSM target is in the same disposable account, and a bare instance ID can accidentally identify production. Simulated workers and stale logical IDs can let all static tests pass while the rehearsal either proves nothing or fails immediately after provisioning billable resources.

**How to apply:** Before every mutation class, call STS through the exact session used for that operation, compare it with an explicitly supplied test account ID plus an immutable production denylist, resolve the target in that account and region, and fail closed unless its run-specific disposable tag matches. Use the actual broker/worker path, durable exactly-once evidence, startup readiness gates, template-to-runner contract tests, scheduler-first shutdown, and paginated residue verification.