---
name: Disposable AWS integration safety
description: Safety boundary for integration tests that use separate AWS principals or mutate remote resources.
---

Destructive AWS integration tests must validate the account identity of every principal they use and must verify an explicit disposable-resource tag before mutating a remote host or service.

**Why:** S3 and SSM operations can use different credential sources. Validating only the storage principal does not prove that the SSM target is in the same disposable account, and a bare instance ID can accidentally identify production.

**How to apply:** Before the first mutation, call STS through the exact session used for that operation, compare it with an explicitly supplied test account ID, resolve the target in that account and region, and fail closed unless its dedicated disposable tag matches.