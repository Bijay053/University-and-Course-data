---
name: Production deployment routing
description: Non-obvious routing constraints for deploying this project to its external AWS host.
---

Inspect the live production repository origin before choosing a push target, and
use the dedicated SSM AWS identity for remote commands rather than the default
storage identity.

**Why:** A previously documented repository target returned “Repository not
found” while the live host and workspace both pointed to a different current
origin. The default AWS identity could access storage but was denied
`ssm:SendCommand`; the dedicated SSM identity worked.

**How to apply:** Before each external production release, read the production
HEAD, working tree, and sanitized origin as the repository owner. Push to that
verified origin, then run commands through the dedicated SSM identity. Never
print credentials, signed URLs, or credential-bearing remotes.

The production storage identity permits object HEAD/get/put/delete but can deny
bucket-versioning and object-version listing calls. For disposable smoke
objects, compute a unique key before upload, HEAD that exact key to recover its
`VersionId` when present, then delete that version directly.

**Why:** A transactional snapshot-storage smoke test reached S3 successfully
but failed during cleanup when it assumed bucket-version metadata permissions.
The exact-version HEAD/delete path works with the production least-privilege
policy and also cleans a remotely committed upload whose response was lost.

**How to apply:** Do not widen S3 permissions merely for deployment smoke
cleanup. Always place cleanup in an outer `finally`, retry it, and verify the
exact key returns 404. Lifecycle rollback must pass
`TransitionDefaultMinimumObjectSize` as a top-level put-bucket-lifecycle
parameter, never inside `LifecycleConfiguration`.