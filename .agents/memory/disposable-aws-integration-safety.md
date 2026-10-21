---
name: Disposable AWS integration safety
description: Safety boundary for integration tests that use separate AWS principals or mutate remote resources.
---

Destructive AWS integration tests must validate the account identity of every principal they use and must verify an explicit disposable-resource tag before mutating a remote host or service.

Infrastructure rehearsals must exercise the real queue/worker semantics and validate runner-to-template resource identifiers; a parked message or source-string assertion is not operational proof.

Offline SDK doubles must preserve the real client's resource-lifecycle contract.

**Why:** Wrapping fake AWS clients in context managers concealed that real
clients only provide `close()`, causing live storage checks to fail before
provisioning despite passing offline tests.

**How to apply:** Use close-capable, non-context-manager doubles and assert
cleanup on both success and failure paths.

Never persist a SigV4 presigned attestation made with temporary credentials: its URL embeds the session token. Use a dedicated asymmetric KMS signing key and pin only its public key/ARN in production.

SSM documents must wait for an explicit bootstrap-complete marker, use the application virtual environment for Python dependencies, and keep readiness/control checks bounded but retryable. Create event schedules disabled and enable them only after the mutation under test and an ownership-tag check. Keep exact proof output free of setup-command noise.

Test the executable bodies of embedded SSM checks locally, not only their source strings. Prefer parameterized SQLite queries in Python over SQL nested inside multiple shell quoting layers.

Hand off a signed rehearsal receipt to its destination within the same operation that creates it; do not rely on an ignored local file remaining available after a long background job.

**Why:** A disposable rehearsal succeeded and deleted its stack, but a workspace recycle removed its ignored local proof before the separate production handoff. The signed proof could not safely be reconstructed, forcing an extra billable rehearsal.

**How to apply:** Validate teardown and signature, then deliver the receipt to the protected destination before ending the background operation. If the operation is interrupted, verify the remote receipt and exact disposable-resource deletion before retrying.

Disposable bucket ownership should be established by the provisioning service,
not by treating a successful client response as the sole evidence of ownership.

Least-privilege archive checks must omit ListBucket as well as remote deletion
and bucket mutation permissions.

**Why:** S3 returns AccessDenied rather than NoSuchKey for absent objects when
the caller lacks ListBucket. A broad harness identity hides this distinction,
so successful archival under that identity does not prove the narrow contract.

**How to apply:** Exercise the real archive with a separate scoped role. Use
conditional creation after a concealed missing-key response and require
verified readback before pruning local evidence. Offline SDK stubs establish
request behavior, not actual IAM enforcement.

New temporary harness permissions need time to propagate before CloudFormation
starts provisioning.

**Why:** An approved exact-bucket grant was readable from IAM immediately, but
CloudFormation still failed CreateBucket with no matching identity permission.
That failed run required full teardown despite the correct policy document.

**How to apply:** After an approved temporary grant, wait for propagation and
check effective permissions before provisioning. Preserve exact policy identity
for removal and independently confirm removal after resource cleanup.

**Why:** S3 bucket creation and tagging are separate calls. A lost create response
or a failure before tagging can leave a bucket that a tag-fenced cleanup routine
must refuse to delete. Provider-managed tagged provisioning and a saved run
identity allow safe recovery without weakening the ownership check.

**How to apply:** For new disposable storage rehearsals, use tagged provisioning,
save non-secret resource identities before creation, and recover killed child
processes with ownership-checked cleanup before independently verifying absence.

**Why:** A real rehearsal passed rotation and worker readiness but failed its final count check because shell escaping reached SQLite literally. Source-presence tests had passed without executing that check.

**How to apply:** Extract the actual template check and exercise success, missing execution, and duplicate execution locally before provisioning another disposable database.

**Why:** S3 and SSM operations can use different credential sources. Validating only the storage principal does not prove that the SSM target is in the same disposable account, and a bare instance ID can accidentally identify production. Simulated workers and stale logical IDs can let all static tests pass while the rehearsal either proves nothing or fails immediately after provisioning billable resources. Cloud-init, package paths, service recovery, and provider capacity are asynchronous; a single readiness probe is brittle, while an enabled schedule can fire before the fixture is safe. A mode-0600 receipt still leaks a credential if it contains an `X-Amz-Security-Token`.

**How to apply:** Before every mutation class, call STS through the exact session used for that operation, compare it with an explicitly supplied test account ID plus an immutable production denylist, resolve the target in that account and region, and fail closed unless its run-specific disposable tag matches. Use the actual broker/worker path, durable exactly-once evidence, startup markers, bounded readiness retries, host-native command names, template-to-runner contract tests, disabled-until-owned schedules, scheduler-first shutdown, and paginated residue verification. Redirect setup chatter when an exact output token is part of the contract. Treat a provider `insufficient-capacity` event as infrastructure failure rather than application proof; inspect stack events and independently verify rollback residue before deciding whether to retry. Emit maintenance proof only after cleanup, sign the canonical payload with KMS, and verify it against a repository-pinned account/key/public key.