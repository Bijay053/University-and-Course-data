# Deploy artifacts

## Read-only inspection of an existing restart smoke sample

Successful ordinary `safe_restart_smoke.py` runs now automatically retain a
private JSON receipt in `/var/lib/university-portal/smoke-receipts/<job-id>.json`
(override with `--receipt-directory`). The same sanitized JSON is printed before
the existing success line. Receipts use the actual persisted DONE payload and
the exact task result and complete per-worker replies accepted by the guards;
they do not dispatch a second sample or inspect workers again.

The receipt follows the `database`, `celery`, `complete_worker_replies`, and
`active_database_counts` structure in
`docs/production/release-sample-lifecycle-evidence-2026-10-08.json`. Invocation
and final worker observation timestamps distinguish it from later inspections.
It does not invent SSM command IDs, journal evidence, or infrastructure identity.
Credential-bearing keys, URL credentials/query strings, and common credential
text are redacted recursively; other DONE fields are retained in full.
Files are created atomically with mode 0600 and never replace earlier evidence.
If receipt persistence fails, the command fails rather than reporting success.
The sample may already have completed: **do not rerun it just to obtain proof**;
inspect that existing job instead. Receipts are local acceptance evidence, not
signed deployment authorization or proof that the whole catalogue is correct.

### Opt-in durable receipt archive

`archive_smoke_receipts.py` archives **existing receipts only** to the existing
approved AWS S3 facility. It never imports the smoke runner, creates a scrape,
changes bucket settings, or deletes remote objects. Nothing is enabled or
scheduled automatically. Production enablement requires separate approval.

After approval, run from `backend-py` using the existing storage AWS identity:

```bash
python -B deploy/archive_smoke_receipts.py \
  --bucket APPROVED_EXISTING_BUCKET \
  --expected-owner APPROVED_12_DIGIT_ACCOUNT \
  --region APPROVED_REGION --keep-local 50
```

The bucket, owner and region must be explicitly supplied; credentials come from
the existing AWS credential chain, not command-line arguments. The bucket must
already have versioning and all four public-access blocks enabled. The command
refuses expiration rules covering `release-smoke-receipts/v1/`, including
noncurrent-version expiry. Existing snapshot-type tag rules do not apply to
these untagged receipts. It does not modify those policies. Grant only the
required bucket configuration reads plus PutObject/GetObject/GetObjectVersion
on this prefix; no delete or bucket-policy mutation permissions are needed.

Receipts are redacted again and stored under a job-ID/content-SHA256 key using
conditional writes, server-side encryption and private bucket access. Each
upload is read back and compared byte-for-byte and against SHA256 metadata.
Retrying after a failure reuses the same object, including an upload whose
response was lost. No new sample is needed.

The newest 50 local receipts are retained by default. Older files are removed
only after their exact remote version is freshly read and verified and the
local file is confirmed unchanged. Unarchived, malformed, changed or
unverifiable receipts remain local even above that limit: preserving the only
copy takes priority over a hard disk cap. A local lock serializes archive runs.
The JSON summary reports failures and the command exits nonzero if any receipt
could not be safely handled. Rerun **the archive command**, never the sample.
Arrange any periodic invocation through the approved production operations
process; no timer or service is installed by this change.

### Disposable archive-storage integration (separate approval required)

`deploy.rehearse_archive_storage` and
`tests/test_archive_storage_aws_integration.py` test **storage only**, with
synthetic receipts. They do not import the application, invoke a smoke runner,
create scrape jobs, contact SSM/EC2, or change production configuration. Ordinary
test runs skip all three live cases.

Do **not** enable the integration without separate approval for billable AWS
execution. It creates one fresh CloudFormation stack containing one private,
versioned, encrypted S3 bucket per case. It borrows an existing dedicated
asymmetric RSA KMS signing key; it never creates or deletes that key. No database,
host, worker, queue, or production archive bucket is used.

Required operator setup, in a separately approved disposable account:

- Explicit `DISPOSABLE_AWS_ACCESS_KEY_ID`, `DISPOSABLE_AWS_SECRET_ACCESS_KEY`,
  and optional `DISPOSABLE_AWS_SESSION_TOKEN`, supplied through the secret store.
  There is no fallback to application credentials.
- `DISPOSABLE_AWS_ACCOUNT_ID`, `PRODUCTION_AWS_ACCOUNT_ID`, and
  `DISPOSABLE_AWS_REGION`. STS must match the disposable account. The supplied
  production ID and the repository's immutable production denylist are rejected.
- `DISPOSABLE_AWS_PROOF_SIGNING_KEY_ID`: an enabled RSA `SIGN_VERIFY` key in
  that account/region. Grant DescribeKey and Sign only on this dedicated key.
- `ARCHIVE_STORAGE_SIGNERS_PATH`: an independently reviewed public-key pin file,
  using the existing rehearsal signer format:
  `{"schema_version":1,"signers":[{"account_id":"DISPOSABLE_ACCOUNT",
  "signing_key_arn":"EXACT_KMS_KEY_ARN","public_key_pem":"PINNED_PUBLIC_PEM"}]}`.
  Do not trust a public key supplied by a proof itself. No private keys or
  presigned URLs are saved.
- `ARCHIVE_STORAGE_PROOF_DIRECTORY`: an existing, operator-controlled persistent
  artifact directory. Configure the approved CI job to collect its state/proof
  files **even on failure**, in the same job; do not rely on pytest temporary
  directories or a later workspace session for evidence handoff. Local generated
  output should be under ignored `generated-artifacts/`.
- Permissions limited to STS identity, the disposable stack names and bucket
  names (`uni-archive-it-*`): CloudFormation create/describe/resource management/
  delete and S3 bucket create/delete, tags, ownership controls, encryption,
  versioning, public-access blocks, lifecycle, listing versions, conditional
  puts, exact-version gets/deletes. Do not grant access to production buckets.
  CloudFormation applies run-specific ownership tags during provisioning;
  cleanup refuses a stack or bucket whose tag does not match.

After separate approval and setup, run:

```bash
cd /root/University-and-Course-data && cd backend-py && \
  RUN_DISPOSABLE_AWS_ARCHIVE_STORAGE=1 PYTHONPATH=. python -m pytest \
  tests/test_archive_storage_aws_integration.py -q -c /dev/null \
  --noconftest --import-mode=importlib -p no:cacheprovider
```

Expect approximately 30–40 minutes for the three serial cases: AWS recommends
15 minutes after first enabling versioning before object writes, and both the
success and post-upload failure cases honor that interval. Cases verify:

- A real S3 `IfNoneMatch="*"` conflict, one durable version, AES256 encryption,
  byte-for-byte readback and matching SHA256 metadata.
- Upload committed to S3 followed by an injected lost response. Retrying the
  archive reuses the stored versions without another upload or scrape sample.
- Real public-access-block/lifecycle configuration refusal, with no receipt
  uploads or local deletion. The public-access test disables only
  `IgnorePublicAcls`; bucket-owner-enforced ownership and `BlockPublicPolicy`
  remain enabled, and no public policy or ACL is ever granted.
- Newest local receipt retention, and preservation of older receipts on actual
  body/metadata corruption or exact-version deletion, injected read failure,
  and a local file changed during verification.

The success case and injected failures after stack creation/upload all run
cleanup. Cleanup lists **every page**, deletes each exact `VersionId` including
delete markers, checks no versions remain, deletes only the tagged owned stack,
and independently verifies both stack and bucket absence. Tests independently
repeat those absence checks; an expected nonzero exit is not cleanup proof.

Only a complete success **after cleanup** writes a mode-0600, atomic KMS-signed
`disposable_archive_storage` proof. It includes run/account/region, resources,
checks and the harness/archive code digest. Its signature is verified against
the pinned public key before writing. `archive_storage_proof.validate_proof`
also checks the run, purpose, current code digest and 24-hour freshness offline.
This proof is not a database refresh or release-smoke acceptance receipt.
Failed cases produce only non-secret state, never a success proof.

On a crash, timeout or unverified cleanup, retain the state JSON. After confirming
the exact approved run/account/region, retry ownership-checked teardown (no
scenarios run and no success proof is created):

```bash
cd /root/University-and-Course-data && cd backend-py && \
  python -m deploy.rehearse_archive_storage \
  --expected-account-id APPROVED_DISPOSABLE_ACCOUNT \
  --production-account-id PRODUCTION_ACCOUNT --region APPROVED_REGION \
  --run-id EXACT_RUN_ID_FROM_STATE --state-output /approved/artifacts/STATE.json \
  --proof-output /approved/artifacts/UNUSED_PROOF.json \
  --proof-signing-key-id APPROVED_KEY --signers /approved/archive-signers.json \
  --cleanup-only --i-understand-this-creates-disposable-aws-resources
```

Do not bypass missing ownership tags or suppress cleanup failures. Investigate
in the disposable account; the harness deliberately will not delete an unowned
resource. No periodic workflow or production configuration is installed.

Offline checks (no AWS operations):

```bash
cd /root/University-and-Course-data && cd backend-py && PYTHONPATH=. python -m pytest \
  tests/test_archive_storage_harness.py tests/test_archive_storage_aws_integration.py \
  tests/test_archive_smoke_receipts.py --import-mode=importlib -q -c /dev/null \
  --noconftest -p no:cacheprovider
```

### Restore an archived receipt onto a replacement host

`restore_smoke_receipts.py` retrieves **existing evidence only**. Obtain separate
approval before accessing production storage. Nothing runs automatically, and
the command never imports the smoke runner, dispatches a sample, queries the
database/workers, writes to S3, changes bucket policies, or deletes versions.

After approval, supply the approved bucket, owning account, region and exact
original sample job ID (no inferred defaults for any of these):

```bash
cd /root/University-and-Course-data && python3 -B backend-py/deploy/restore_smoke_receipts.py \
  --bucket APPROVED_EXISTING_BUCKET \
  --expected-owner APPROVED_12_DIGIT_ACCOUNT \
  --region APPROVED_REGION \
  --job-id safe_restart_smoke_ORIGINAL_32_LOWERCASE_HEX_ID
```

Credentials use the normal AWS chain; never pass credentials on the command line.
The read-only identity needs `s3:ListBucketVersions` restricted to the
`release-smoke-receipts/v1/` prefix and `s3:GetObjectVersion` on that prefix.
Every request includes `ExpectedBucketOwner`. Recovery does not require changing
or revalidating lifecycle policies: an existing noncurrent version can still be
recovered even if a delete marker hides the key.

All pages of the exact job prefix are searched. When multiple content hashes or
object versions match, the command fails closed, **never choosing the newest**.
Use the approved archive inventory to supply `--sha256 ORIGINAL_CONTENT_HASH`
and/or `--version-id ORIGINAL_S3_VERSION_ID` until exactly one version matches.
Missing, inaccessible, oversized, corrupted or ambiguous evidence is an error,
not a reason to start another sample. Listings are bounded to 100 pages.

The selected version is fetched explicitly. Its bytes must match the SHA256 in
both its key and metadata, its returned version must match, and the receipt's
kind, schema and database/Celery job identities must agree with the requested job.
Duplicate JSON keys and conflicting task-result job IDs are rejected. SHA256
checks detect corruption; they are not a signature or proof of current release
health. This restores historical acceptance, not acceptance of the new host.

The default destination is `/var/lib/university-portal/smoke-receipts`; use
`--directory PRIVATE_DIRECTORY` to change it. The destination must be owned by
the invoking user with mode 0700 and must not be a symlink. The receipt is
published atomically as `<job-id>.json`, mode 0600, under the archive's local
lock. Existing byte-identical private files are left untouched (including their
mtime). Different bytes, even formatting-only differences, unsafe permissions,
symlinks and nonregular files fail without overwriting local evidence.

The archived bytes, revision, original acceptance and worker timestamps are
preserved exactly. The separate JSON command summary says
`operation: restore_smoke_receipt`, `new_acceptance: false`, and reports
`restored` or `already_present` with a **restoration check** time, never a new
acceptance time. Retain that summary in the operator's recovery log; it is not
inserted into the original receipt. Failures print fixed reasons without raw
provider errors or receipt contents. After any failure, retry restoration only;
if the local publish succeeded before a later error, retry is idempotent.

Use `inspect_restart_smoke.py`, **not** `safe_restart_smoke.py`, when the sample
already exists. Obtain the explicit job and Celery task IDs from the original
run. Obtain the complete expected worker nodenames from the deployment inventory,
not by assuming that every expected worker answered ping.

With the relevant production approval, run from `backend-py` on the deployed host
using its normal database/Redis environment and Python interpreter:

```sh
PYTHONPATH=. .venv/bin/python -B deploy/inspect_restart_smoke.py \
  --job-id safe_restart_smoke_EXISTING_JOB_ID \
  --task-id EXISTING_CELERY_TASK_UUID \
  --expected-worker uni-celery@EXPECTED_HOST
```

Repeat `--expected-worker` for each expected worker. Optional `--timeout` sets
each Celery inspection timeout (default 10 seconds, maximum 60).
`--expected-skip-reason` defaults to `domestic_only`.
The command needs permission to read the service MainPIDs' release environment.
It never dispatches, recovers, restarts, commits, or creates a sample. All database
reads use one repeatable-read, read-only transaction; engine disposal is awaited
in the same event loop even on failure.

JSON stdout contains persisted DONE (including sequence/time), actual staged-row
count, job counters and sample revision, current Git/release-file/process revisions,
the smoke-script hash, AsyncResult identity/state/result, unabridged current
ping/active/reserved/scheduled replies, and active database counts. Missing results,
identity mismatches, incomplete replies, current non-idleness, or invalid sample
outcomes exit 1 with validation errors. Success exits 0. Operational failures also
exit 1 with an error type, without exposing credentials or a traceback.
No result-backend wait, result deletion, or worker state mutation is performed.

This follows `../docs/production/release-sample-lifecycle-evidence-2026-10-08.json`
but is explicitly a **later observation**, with observation start/end timestamps.
It is not a simultaneous distributed snapshot and cannot retroactively certify
the original acceptance time or earlier idle state. A historical sample may have
an older revision than the currently deployed code; both revisions are retained
without claiming that the sample tested the newer release. This command does not
replace the original smoke gate or authorize a subsequent restart.

Service files, secure credential rotation tools, plus the cutover runbook
in `../README.md`.

| File | Where it goes on production |
|---|---|
| `uni-api-py.service` | `/etc/systemd/system/uni-api-py.service` |
| `uni-celery.service` | `/etc/systemd/system/uni-celery.service` |
| `nginx.conf` | `/etc/nginx/sites-available/default` (after backup of current) |
| `install_openai_fallback_via_ssm.py` | Run from a trusted deployment workspace; do not copy to production |
| `install_snapshot_storage_via_ssm.py` | Securely install production snapshot storage and run a disposable round-trip check |
| `rotate_openai_fallback_via_parameter_store.py` | Routine credential rotation from a trusted deployment workspace |
| `openai-parameter-store-iam.yaml` | One-time least-privilege IAM and KMS setup |
| `rotate_snapshot_storage_via_parameter_store.py` | Routine snapshot credential rotation from a trusted deployment workspace |
| `snapshot-parameter-store-iam.yaml` | One-time least-privilege snapshot IAM, KMS, and fixed SSM document setup |
| `refresh_database_credentials_via_secrets_manager.py` | Requests the fixed host-side refresh of the RDS-managed database credential |
| `database-secret-rotation-iam.yaml` | One-time least-privilege fixed-secret and SSM-document IAM setup |
| `database-secret-refresh-rehearsal.yaml` | Isolated, tagged disposable RDS/SSM/Scheduler refresh fixture |
| `rehearse_database_secret_refresh.py` | Explicitly opt-in disposable refresh rehearsal orchestrator |
| `deploy_database_secret_rotation_stack.py` | Revision-fenced deployment and post-update verification for the database rotation stack |
| `prove_database_refresh_alert.py` | Publishes one disposable sanitized database-refresh failure alert and proves repeat suppression |
| `prove_database_refresh_alert_delivery.py` | Temporarily triggers and restores the fixed delivery-failure alarm |
| `reconcile_generated_configs.py` | Preserves verified generated recipe collisions and audits or removes fully superseded runtime overlays |
| `release_revision_fence.sh` | Verifies and fast-forwards only an exact reviewed Git revision |

## Guarded application release

Invoke the production release entrypoint with the exact currently deployed
revision, the exact intended `origin/main` revision, and the 12-digit disposable
AWS account used by the mandatory rehearsal proof:

```bash
cd /opt/university-portal
sudo backend-py/deploy/guarded_release.sh \
  "$PREDECESSOR_FULL_SHA" \
  "$TARGET_FULL_SHA" \
  "$DISPOSABLE_AWS_ACCOUNT_ID"
```

Both revisions must be lowercase 40-character Git commit IDs. Abbreviated and
symbolic refs are rejected. The checked-out revision must equal the predecessor,
the predecessor must be an ancestor of the target, and fetched `origin/main`
must equal the target. The script fetches and checks the remote tip again after
release preparation and immediately before the fast-forward checkout by calling
the same narrowly scoped revision-fence helper used by the integration tests.
The helper performs only Git revision verification and optional fast-forward
checkout; it cannot run proofs, restart services, or clean release state. If main
advanced, it exits before changing the checkout or restarting services; rerun
with the same predecessor and the newly reviewed exact remote tip.

Do not bypass the entrypoint's disposable-account proof, worker/job idle gates,
tracked and generated config reconciliation, release-identity smoke checks,
public smoke checks, or EXIT cleanup.

### Per-release acceptance manifest

Each guarded attempt retains a private
`/var/lib/university-portal/release-manifests/release_<id>/manifest.json`.
`RELEASE_MANIFEST=...` prints its location before the first sample. The directory
contains that invocation's original `preflight/` and `post_restart/` ordinary
smoke receipts. These are separate from standalone smoke receipts; neither
rollback nor the standalone receipt archive's local pruning removes them.

The manifest helper is extracted from the exact reviewed target before checkout
and kept outside the checkout until cleanup. The preflight sample certifies the
predecessor; after restart, service-identity and public frontend checks, a **new
ordinary sample** certifies the target. Each phase requires successful command
exit, exactly one receipt in a fresh private directory, matching revision and
job/task identities, timestamps within that invocation, canonical DONE outcome,
and complete idle-worker/database evidence. Later read-only observations from
`inspect_restart_smoke.py` are never accepted as replacements.

The allowlisted manifest records both full revisions, unique attempt identity,
phase times/status, original job/task IDs, relative receipt filenames and SHA256
hashes of the **exact retained file bytes**. It contains no environment values,
commands, exception details, URLs, or sample payloads. Before final completion,
both receipts are re-read and their hashes and identities checked again.

`preflight_passed: true` is not release completion. `post_restart_passed` also
is not completion: only after all remaining release checks, reconciliation
finalization and successful EXIT cleanup does `status: completed` /
`release_completed: true` permit the
`DEPLOYED_RELEASE` message. A normal failure records `status: failed`, preserves
any accepted phase references and original receipts, and runs the existing
rollback. Abrupt termination can leave a running phase; that is **not** proof of
completion. Rollback can restore frontend/config state without reverting backend
HEAD; inspect the actual current release before any subsequent attempt.

Do not retry samples to repair missing evidence or relabel an old receipt.
Preserve the entire attempt directory, including evidence from failed attempts.
The existing receipt archive is for standalone receipts only; it does not
currently archive release manifests or their paired copies.

## Tracked operator recipe preservation

The guarded release accepts tracked worktree edits only when every change is a
modified, regular YAML file directly under
`backend-py/scraper_config/unis/`. Staged changes, deleted or renamed recipes,
symlinks, edits outside that directory, and target revisions that remove an
edited recipe or change it from a regular non-executable Git blob all block the
release before pull.

Before the fast-forward pull, the target revision's reconciliation helper
records the operator, current-HEAD, and target-blob SHA-256 digests in a
temporary manifest, moves each operator recipe to a repository-private backup,
and materializes the current tracked blob. The ordinary clean-tree gate then
runs unchanged. Immediately after the pull and before any service restart, the
helper accepts only the exact current-HEAD or target blob at each path, restores
the operator bytes without overwriting a concurrently recreated leaf, and
verifies their digests. Backups are deleted only after service and public smoke
verification. Finalization revalidates the complete source and backup set, then
atomically renames the manifest to a committed marker before best-effort backup
deletion. A deletion failure can leave stale backup files and the committed
marker, but cannot turn a completed restoration back into a partial rollback.

The EXIT cleanup repeats the same fenced restoration after an aborted release.
If a recipe or backup changes after its snapshot, restoration fails closed:
the scrape consumer remains paused and the manifest, backup, and any race
quarantine are retained for manual recovery. If consumer cancellation cannot be
confirmed during failed cleanup, the Celery service is stopped. Unknown dirty
files are never normalized or overwritten.

## Generated config overlay cleanup

Tracked university recipes override matching settings in verified generated
runtime overlays. The supported production release flow in
`backend-py/deploy/guarded_release.sh` automatically runs this read-only audit after it has
checked out and verified the target revision, before restarting services. Its
output includes `REDUNDANT_CONFIG_OVERLAY_COUNT` and one
`REDUNDANT_CONFIG_OVERLAY_PATH` line per finding.

Run the same audit manually with:

```bash
cd /opt/university-portal
PYTHONPATH=backend-py python backend-py/deploy/reconcile_generated_configs.py \
  audit-overlays --repo-root .
```

The read-only command emits one JSON record per verified overlay whose every
YAML leaf path is present in the tracked recipe with the same filename. Values
may differ because the tracked value is authoritative. It ignores unverified,
malformed, unexpectedly named, and untracked files. Review the records, then
remove only the currently proven redundant overlays:

```bash
PYTHONPATH=backend-py python backend-py/deploy/reconcile_generated_configs.py \
  cleanup-overlays --repo-root .
```

Cleanup repeats the full audit and digest checks immediately before each
deletion. Any generated-only setting retains its overlay, and unknown files are
never deleted.

An audit or output-format failure emits
`REDUNDANT_CONFIG_OVERLAY_AUDIT_WARNING=failed_non_blocking`; it does not block
the service restart. The audit helper independently checks Git connectivity and
blocks restart with exit code 42 only when `git fsck` output contains recognized
object-corruption evidence. An unavailable or inconclusive check remains
warning-only.
The automatic release audit never invokes `cleanup-overlays`.

## Refresh the RDS-managed database credential

### Disposable AWS refresh rehearsal

Before changing the production refresh transaction, rehearse it only in a
separate non-production account with two private subnets in different AZs. This
creates a uniquely tagged private PostgreSQL instance whose master secret is
RDS-managed, an SSM-only test host, a dedicated five-minute Scheduler group,
and a local Redis/Celery durable-work probe. A preparation SSM document waits
for cloud-init and SSM readiness, verifies the isolated services, cancels the
`scrape` consumer, and enqueues exactly one rehearsal-only
`scrape.university` task. The scheduled refresh applies the production-like
Celery active-task fence, then resumes consumption after both isolated
`uni-api-py` and real `uni-celery` units restart. Success requires exactly one
durable SQLite execution record and an empty Redis queue. It validates STS
identity and every
mutation target's disposable ownership tag; it refuses configured production
accounts and never prints a secret. It rotates the fixture, requires
Scheduler-origin SSM evidence, verifies the restart and RDS-CA
certificate-verifying `SELECT 1`, proves queued work survived, and deletes the
stack in `finally`.

```bash
export RUN_DISPOSABLE_AWS_DATABASE_REFRESH_REHEARSAL=1
python backend-py/deploy/rehearse_database_secret_refresh.py \
  --expected-account-id "$DISPOSABLE_AWS_ACCOUNT_ID" \
  --production-account-id "$PRODUCTION_AWS_ACCOUNT_ID" --vpc-id "$TEST_VPC_ID" \
  --private-subnet-id "$TEST_PRIVATE_SUBNET_A" \
  --second-private-subnet-id "$TEST_PRIVATE_SUBNET_B" \
  --proof-output runtime-proofs/database-refresh-rehearsal.json \
  --proof-signing-key-id "$DISPOSABLE_AWS_PROOF_SIGNING_KEY_ID" \
   --handoff-production-instance-id "$UNIVERSITY_PORTAL_INSTANCE_ID" \
  --i-understand-this-creates-disposable-aws-resources
```

The runner reads only `DISPOSABLE_AWS_ACCESS_KEY_ID`,
`DISPOSABLE_AWS_SECRET_ACCESS_KEY`, and optional
`DISPOSABLE_AWS_SESSION_TOKEN`. It never falls back to the production
`AWS_SSM_*` identity or the default AWS credential chain. The non-secret proof
is written atomically only after Scheduler is disabled, the stack is deleted,
the exact run-tag residue search is empty, and the RDS-managed secret is gone.
With `--handoff-production-instance-id`, the same run rechecks the signed
receipt, disposable STS identity, deleted stack and run-tag residues, then
checks the **separate** production STS identity and running instance before
installing the receipt. Supply dedicated `AWS_SSM_ACCESS_KEY_ID` and
`AWS_SSM_SECRET_ACCESS_KEY` (and `AWS_SSM_SESSION_TOKEN` when applicable);
there is no production default-credential fallback. The production account
argument must be the protected account ID. The production SSM principal needs
`sts:GetCallerIdentity`, `ec2:DescribeInstances`, and `ssm:SendCommand` /
`ssm:GetCommandInvocation` to that exact instance and `AWS-RunShellScript`;
the disposable principal needs read-only CloudFormation, resource-tag and EC2
inspection in addition to the rehearsal permissions. The host must already
have this template and pinned signer, Python 3 and OpenSSL. Only a temporary
public certificate, encrypted receipt, and fixed status cross SSM; command
output never includes receipt JSON or credentials. Host validation runs before
an fsynced, mode-0600 atomic install at
`/etc/university-portal/database-refresh-rehearsal-proof.json`. The guarded
release still validates the host receipt independently with its existing
24-hour limit; handoff does not bypass that gate.

If handoff fails or the workspace is interrupted, **do not rerun the billable
rehearsal immediately**. Check that the protected host receipt passes the
existing release-gate validation and check disposable teardown. If the
receipt is absent but the ignored local proof survives, retry only the handoff:

```bash
python backend-py/deploy/handoff_database_refresh_rehearsal.py \
  --proof runtime-proofs/database-refresh-rehearsal.json \
  --disposable-account-id "$DISPOSABLE_AWS_ACCOUNT_ID" \
  --production-account-id "$PRODUCTION_AWS_ACCOUNT_ID" \
  --production-instance-id "$UNIVERSITY_PORTAL_INSTANCE_ID" \
  --region ap-south-1
```

This retry refuses stale or invalid signatures, a live stack, tagged residues,
wrong accounts or a wrong-region proof. If both the host receipt and local
proof are lost, no receipt can be reconstructed; investigate teardown and
only then decide whether a new rehearsal is needed. Do not copy a receipt
through SSM stdout, logs, command comments, or a credential-bearing URL.

Provision one persistent asymmetric KMS key in the disposable account with
`KeyUsage=SIGN_VERIFY` and an RSA key spec. Restrict the rehearsal principal to
`kms:DescribeKey` and `kms:Sign` on that exact key. Export only its public key:

```bash
aws kms get-public-key \
  --key-id "$DISPOSABLE_AWS_PROOF_SIGNING_KEY_ID" \
  --query PublicKey --output text |
  base64 --decode |
  openssl pkey -pubin -inform DER -outform PEM \
    -out /tmp/database-refresh-rehearsal-public.pem
```

Verify `aws kms describe-key` reports the expected disposable account, region,
key ARN, `SIGN_VERIFY`, RSA key spec, and enabled state. Then add exactly one
entry containing that account ID, full key ARN, and public PEM to
`deploy/database-refresh-rehearsal-signers.json`. Public-key material is not a
secret. Review this pin as a production trust change; never populate it from a
proof file or caller-supplied account.

Do not use a production account ID, production VPC, or production subnets. The
known production account `905043442097` is rejected immutably as well as by the
required CLI production-account guard. The VPC must be non-default and it and
both private, distinct-AZ subnets must carry
`university-portal:disposable-network=<disposable-account-id>`; each subnet
must have an active NAT gateway default route for package and RDS-CA downloads.
Teardown disables Scheduler first, still attempts stack deletion if that fails,
paginates an exact run-tag residue search, and proves the RDS-managed secret is
gone while retaining both primary and cleanup errors. The
script is deliberately not a normal deployment command and performs no work
without its long opt-in flag.

`DATABASE_URL` is not a deployment input on production. `.env` holds
non-secret, general application defaults and `.release.env` holds only immutable
release metadata. Both services load `/etc/university-portal/database.env`
last; it is a root-only (`0600`) generated file containing the active database
URL and must never be committed, copied into either repository environment
file, or edited manually.

Deploy `database-secret-rotation-iam.yaml` only through the checked-in wrapper,
with the **exact ARN** of the existing RDS-managed master secret and the production instance ID:

```bash
python backend-py/deploy/deploy_database_secret_rotation_stack.py \
  --instance-id "$UNIVERSITY_PORTAL_INSTANCE_ID" \
  --database-secret-arn "$DATABASE_SECRET_ARN" \
  --alert-email "$DATABASE_REFRESH_ALERT_EMAIL" \
  --region ap-south-1
```

For every later update, also pass the revision reported by the preceding
successful deployment:

```bash
python backend-py/deploy/deploy_database_secret_rotation_stack.py \
  --instance-id "$UNIVERSITY_PORTAL_INSTANCE_ID" \
  --database-secret-arn "$DATABASE_SECRET_ARN" \
  --alert-email "$DATABASE_REFRESH_ALERT_EMAIL" \
  --expected-current-revision "$CURRENT_DEPLOYED_TEMPLATE_REVISION" \
  --region ap-south-1
```

For the one-time upgrade of an existing stack created before revision fencing,
run only from the administrator-controlled infrastructure pipeline and opt in
explicitly:

```bash
python backend-py/deploy/deploy_database_secret_rotation_stack.py \
  --instance-id "$UNIVERSITY_PORTAL_INSTANCE_ID" \
  --database-secret-arn "$DATABASE_SECRET_ARN" \
  --alert-email "$DATABASE_REFRESH_ALERT_EMAIL" \
  --adopt-unversioned-stack \
  --region ap-south-1
```

This flag is accepted only when the stack has no `DeployedTemplateRevision`
output and cannot be combined with `--expected-current-revision`. After that
single update creates the output, the wrapper rejects the adoption flag and
requires the exact current revision on every update.

The wrapper takes a conditional lease in the stack's DynamoDB table before it
reads the current revision and holds that lease through final verification. It
also rejects an in-progress stack and an expected revision that does not exactly
match the stack output. Thus two workspaces based on the same older revision
cannot both deploy: one holds the lease, and after it finishes the other fails
its stale-revision check. Success is reported only after the stack output and
the default SSM document version, repository revision, and checked-in command
content hash all match. Do not use a bare
`aws cloudformation deploy` command for this stack. The wrapper does not accept
an alternate stack name or caller-supplied revision. It refuses a dirty
repository and verifies the submitted template byte-for-byte against the
immutable checked-out Git HEAD before contacting AWS. That single verified Git
blob is then reused for hashing, CloudFormation submission, and post-deploy
document verification. Existing operator-set values for deployment user,
instance role, schedule state, and alert deduplication interval are retained
with CloudFormation `UsePreviousValue`; the wrapper changes only its explicit
inputs. Run it only through the administrator-controlled
infrastructure deployment identity and protected source/change-approval
pipeline. Do not grant the routine `DeploymentRefreshPolicy` identity
CloudFormation update or execution-role permissions: an updater that can submit
arbitrary template bodies to this IAM-managing stack could escalate its own
privileges.
The very first stack creation has no lock table yet; CloudFormation's atomic
create-by-name operation serializes that bootstrap. On an existing installation
that predates this safeguard, use the one-time adoption command above; the
administrator-controlled deployment identity must be able to write the fixed
lock item. Routine credential refreshes continue to use the scoped deployment
identity; stack-template updates do not.

The instance
role receives only `secretsmanager:GetSecretValue` on that ARN. The deployment
identity cannot read the secret and can only invoke the fixed
`university-portal-database-credential-refresh` SSM document for that
instance—do not add wildcard Secrets Manager or arbitrary Run Command access.
The stack also creates a fixed EventBridge Scheduler schedule that checks the configured
secret every five minutes. An unchanged secret version is a no-op; a new
version runs the atomic restart and smoke transaction below.

The stack routes failed, timed-out, or cancelled executions of only that fixed
SSM document on only the configured production instance to an encrypted SNS
topic. `AlertEmail` is a required, masked stack parameter; set it to the operator
mailbox when creating or updating the stack, then confirm the AWS subscription
email. Alert messages contain only the
instance ID, document name, failure status, and whether the event is a test;
they never include command output or credentials. A DynamoDB conditional write
suppresses repeated alerts for the same instance and document for 60 minutes by
default (`AlertDeduplicationMinutes` can be 5–1440). If SNS publication fails,
the deduplication record is removed so EventBridge can retry instead of hiding
the failure.

### First installation (required order)

The database environment is intentionally mandatory. For the first cutover,
copy both updated unit files and run `systemctl daemon-reload`, but **do not
restart either service yet**. The still-running old API retains the last
known-good URL in its process environment. Then invoke the fixed refresh command
below. The document first proves both loaded unit definitions require
`database.env`, securely captures the running API URL as rollback material when
the file does not exist yet, creates the managed file, and only then restarts
both services. If any check fails, it restores that captured URL and starts both
new units with the last-known-good configuration.

After RDS rotates its managed secret, invoke the refresh from a trusted
deployment workspace:

```bash
python backend-py/deploy/refresh_database_credentials_via_secrets_manager.py \
  --instance-id "$UNIVERSITY_PORTAL_INSTANCE_ID" \
  --region ap-south-1
```

No password is accepted by this command, passed as an SSM parameter, printed,
or placed in command history. On the host, the fixed document reads the secret
directly, atomically replaces `database.env`, restarts API and Celery together,
checks service health and effective process configuration, then runs a real
TLS `SELECT 1`. Any failure restores the last-known-good file and restarts both
services before returning a sanitized operational error. Application engines
always use certificate-verifying PostgreSQL TLS; URL `sslmode` flags cannot
disable it.
The refresh client resolves the default SSM document once, invokes that exact
numeric version, and verifies the same content after the host command succeeds;
an infrastructure update cannot silently switch the command mid-invocation.

Store sanitized production refresh evidence under `backend-py/runtime-proofs/`
when an out-of-band infrastructure repair is required. Evidence may include the
fixed document name/version and content hashes, principal and instance ARNs,
sanitized success markers, TLS smoke outcome, and read-only schema-preflight
outcome. It must never contain the secret ARN, database URL, password, command
stdout/stderr, or environment-file contents. Mark direct IAM repairs as
requiring CloudFormation reconciliation so a later stack update cannot silently
restore stale policy.

The document also maintains a lexically-last systemd drop-in for both services
so older environment drop-ins cannot override the managed database credential
file.
It caches AWS's official global RDS CA bundle, combines it with the operating
system trust store, and supplies that combined bundle through `SSL_CERT_FILE`;
hostname and certificate verification remain enabled.

After installing or updating the stack and confirming the SNS subscription,
prove the end-to-end notifier and rate limit with a disposable test event:

```bash
python backend-py/deploy/prove_database_refresh_alert.py \
  --instance-id "$UNIVERSITY_PORTAL_INSTANCE_ID" \
  --region ap-south-1
```

The command puts two identical events with a unique nonce onto the default
EventBridge bus. A dedicated test rule invokes the same notifier; the first
event must publish a clearly labelled test alert and the second must be
suppressed. The command observes only the non-sensitive publish/suppression
markers in the deduplication table. Test events use a separate, unique key, so
the proof is repeatable and cannot suppress a real production failure alert.
The deployment identity cannot invoke the Lambda directly. The function rejects
an unexpected event source, instance, document, or non-failure status before
writing or publishing.

### Alert-delivery failure handling

Both EventBridge target-delivery failures and asynchronous Lambda execution
failures are retained in the encrypted
`university-portal-database-refresh-alert-dlq` queue after bounded retries.
EventBridge input transformers ensure the Lambda and its asynchronous failure
destination receive only the allowlisted source, detail type, instance ID,
document name, status, and disposable test nonce. An EventBridge target-delivery
DLQ record contains the original AWS SSM command-status event plus delivery
metadata rather than the transformed target input. That AWS event schema
contains command identity/status metadata but never command stdout, stderr,
parameters, environment, or credentials. Tests lock this distinction so the
transformer is not incorrectly treated as protection for the EventBridge DLQ.

The SNS topic and SQS queue use a dedicated rotating customer-managed KMS key.
Its key policy allows only account administration, the database-refresh
EventBridge rule prefix for DLQ encryption, and the database-refresh CloudWatch
alarm prefix for direct alarm publication. The notifier role receives only
`kms:GenerateDataKey` and `kms:Decrypt` on this key.

The `university-portal-database-refresh-alert-delivery-failed` CloudWatch alarm
enters ALARM when the queue has any visible message. A second alarm,
`university-portal-database-refresh-notifier-errors`, detects errors sustained
across two five-minute periods. Both publish directly to the same encrypted SNS
operator topic and contain only static resource/metric metadata. Dead-letter
messages are retained for 14 days for investigation.

After confirming the SNS subscription, prove the secondary alarm-to-operator
path without creating or consuming a real dead-letter message:

```bash
python backend-py/deploy/prove_database_refresh_alert_delivery.py \
  --region ap-south-1
```

The proof refuses to run while the delivery alarm is already in ALARM, sets only
that fixed alarm to a clearly labelled disposable ALARM state, waits until AWS
reports the transition, and restores the exact prior state in `finally`. The
deployment identity can describe and set only this alarm. The state reason and
alarm metadata are static and contain no command output, event payload, or
credentials.

## Rotate the OpenAI fallback routinely

Both services load `/etc/university-portal/openai.env` after the general
application environment. The file is optional so deployments without the
fallback still start, and is created mode `0600` when fallback credentials are
installed.

An AWS administrator must deploy `openai-parameter-store-iam.yaml` once. It
creates a rotating customer-managed KMS key, a fixed-purpose SSM rotation
document, and grants the deployment user write access to exactly one
SecureString:

- `/university-portal/openai/configuration`

The SecureString is one JSON configuration bundle, so the key and URL cannot
be observed as a partially updated pair. It is rotation input, not a claim
about which value is currently active. The production instance role can read
only that bundle and can decrypt it only through Parameter Store. The
deployment user can encrypt but cannot read either value. Supply the production
instance ID when creating the stack; the default principal names match the
current production setup.

For each routine rotation, inject `OPENAI_API_KEY` (and optional
`OPENAI_BASE_URL`) into the deployment process environment through the CI
secret store, then run:

```bash
python backend-py/deploy/rotate_openai_fallback_via_parameter_store.py \
  --instance-id "$UNIVERSITY_PORTAL_INSTANCE_ID" \
  --region ap-south-1
```

The script writes the encrypted bundle and invokes only the fixed-purpose SSM
document (never arbitrary shell). The document owns the complete host
transaction: it backs up the prior environment, installs the new one, checks
the API health endpoint, and verifies that both running process environments
contain the new values. Its exit trap restores and restarts the prior
environment on any failure before successful verification. Never place a
credential in a command-line option, shell assignment, redirected file, or
debug trace. The script prints status only.

## Emergency envelope-encrypted installation

If Parameter Store or its scoped IAM setup is unavailable, retain this
envelope-encrypted installer as the emergency fallback:

```bash
python backend-py/deploy/install_openai_fallback_via_ssm.py \
  --instance-id "$UNIVERSITY_PORTAL_INSTANCE_ID" \
  --region ap-south-1
```

For an external production host, set `OPENAI_API_KEY` to a direct OpenAI API
key. The installer maps it to the service's existing
`AI_INTEGRATIONS_OPENAI_API_KEY` setting and uses
`https://api.openai.com/v1` unless `OPENAI_BASE_URL` is set. Replit-provisioned
`AI_INTEGRATIONS_OPENAI_*` values must not be copied to external hosts because
their proxy endpoint is loopback-only outside a Replit runtime.

The production host generates a temporary private key and returns only its
public certificate. SSM command history receives CMS ciphertext, never either
plaintext value. The host decrypts directly into the root-only environment
file, deletes the temporary key material, installs late-loading systemd
drop-ins, and restarts both services.

The caller needs `ssm:SendCommand` and `ssm:GetCommandInvocation` for the target
instance and `AWS-RunShellScript`. OpenSSL must be installed on both machines.
If `AWS_SSM_ACCESS_KEY_ID` and `AWS_SSM_SECRET_ACCESS_KEY` are set, the installer
uses that dedicated deployment principal; otherwise it uses the default AWS
credential chain.

## Rotate snapshot storage routinely

An AWS administrator must deploy `snapshot-parameter-store-iam.yaml` once. It
creates a dedicated rotating KMS key, one SecureString bundle at
`/university-portal/snapshot-storage/configuration`, a fixed-purpose SSM
rotation document, and narrowly scoped deployment-user and instance-role
policies. Supply the production instance ID to the stack. Do not add broader
parameter read or command permissions to the deployment identity.

For each routine rotation, inject `AWS_S3_BUCKET_NAME`, `AWS_S3_REGION`,
`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, and optional
`AWS_S3_ENDPOINT_URL` into the deployment process through the CI secret store:

```bash
python backend-py/deploy/rotate_snapshot_storage_via_parameter_store.py \
  --instance-id "$UNIVERSITY_PORTAL_INSTANCE_ID" \
  --region ap-south-1
```

The client writes one encrypted, versioned JSON bundle so a partial credential
set can never become active. It then invokes only the fixed-purpose SSM
document, with only the non-secret SecureString version as a command parameter.
The host takes an exclusive rotation lock, verifies that exact parameter version
is still current before and after the transaction, atomically
replaces the existing mode-`0600` environment file, restarts API and worker
together, verifies the exact effective values in both running processes, and
runs a disposable write/read/delete storage canary. Any failure restores the
prior environment and restarts both services before the command reports
failure. Output contains status only.

Never put a credential in a command-line option, shell assignment, redirected
file, debug trace, CI log, or arbitrary SSM Run Command. Parameter Store is the
routine path; the installer below remains the documented emergency path when
the scoped rotation control plane is unavailable.

## Emergency snapshot-storage installation

Run the snapshot installer only from a trusted deployment workspace whose
process environment contains `AWS_S3_BUCKET_NAME`, `AWS_S3_REGION`,
`AWS_ACCESS_KEY_ID`, and `AWS_SECRET_ACCESS_KEY`:

```bash
python backend-py/deploy/install_snapshot_storage_via_ssm.py \
  --instance-id "$UNIVERSITY_PORTAL_INSTANCE_ID" \
  --region ap-south-1
```

The installer uses the same host-generated-certificate envelope transfer as the
emergency OpenAI installer. It atomically creates the mode-`0600`
`/etc/university-portal/snapshot-storage.env`, adds late-loading systemd
drop-ins for both application services, restarts them, verifies each process
received the exact configuration, and confirms the API logged snapshot storage
as enabled. Before committing the transaction it uploads, downloads, and
deletes a harmless uniquely named test snapshot. On failure it restores the
previous environment and drop-ins and restarts both services.

## Record the deployed release

Both application services require
`/opt/university-portal/backend-py/.release.env`. Create it from the
immutable revision being deployed, after checking out that revision and before
restarting either service:

```bash
cd /opt/university-portal

# A packaging pipeline that does not include .git must export RELEASE_REVISION
# to the immutable commit/build revision before running these commands.
deployed_revision="${RELEASE_REVISION:-$(git rev-parse --verify HEAD)}"
test -n "$deployed_revision"

release_env="$(mktemp backend-py/.release.env.XXXXXX)"
printf 'RELEASE_REVISION=%s\n' "$deployed_revision" > "$release_env"
chmod 0644 "$release_env"
mv -f "$release_env" backend-py/.release.env
```

The release file is separate from `.env` so copying an older application
configuration cannot silently restore stale revision metadata. It is loaded
after `.env`, so the value tied to this deployment is authoritative.

## Install and restart

`uni-celery.service` uses an idle-aware stop helper. It first stops the named
worker from consuming `scrape`, then checks its reserved, scheduled, and active
tasks in transition-safe order before sending the normal warm-shutdown signal.
If all three are empty, it allows 10 seconds for a clean exit and then removes
only process identities (PID plus kernel start time) captured from that worker
tree, preventing leaked pool/thread processes from consuming the full stop
timeout. If quiescing or inspection fails, or any task exists, it fails safe:
no early cleanup occurs and systemd retains the full 90-second graceful-stop
window. Restarting the unit restores consumption. The API uses its normal
prompt Gunicorn shutdown.

To diagnose stop latency without combining both units into one timing, first
pause the `scrape` consumer and verify `celery inspect active` reports
`- empty -`; then time `systemctl stop` and recover each unit separately. Always
start and health-check a stopped unit before testing the next one.

After copying the service and nginx files:

```bash
systemctl daemon-reload
systemctl enable uni-api-py uni-celery

# On the first database-credential cutover, run the fixed refresh command from
# the section above here. It creates the mandatory database.env before restart.
test -s /etc/university-portal/database.env

smoke_since="$(date --iso-8601=seconds)"
systemctl restart uni-api-py uni-celery
nginx -t && systemctl reload nginx
```

## Release-identity smoke check

Confirm both processes started with the exact revision written above:

```bash
cd /opt/university-portal/backend-py
PYTHONPATH=. python deploy/safe_restart_smoke.py \
  --release-identity-only \
  --journal-since "$smoke_since" \
  --release-identity-warning-seconds 5 \
  --release-identity-regression-multiplier 2 \
  --release-identity-regression-min-samples 3 \
  --release-identity-regression-history-records 10 \
  --release-identity-timeout-seconds 15
```

The command first requires each live service process environment to contain the
exact full `RELEASE_REVISION`. It then retries each service's exact
`release_revision=<revision>` startup line for up to 15 seconds, allowing normal
Gunicorn/Celery startup delay without accepting a missing or mismatched
identity. Do not complete the deployment if this check fails. A package without
`.git` is supported as long as its deployment pipeline supplies
`RELEASE_REVISION`.

The default warning threshold is 5 seconds and can be changed with
`--release-identity-warning-seconds`. A matching startup line found after that
threshold emits one sanitized warning containing only the service name and
elapsed seconds. The match still succeeds unless it exceeds the independent
`--release-identity-timeout-seconds` hard limit.

Each service is also compared only with its own timings from the newest
successful deployment records. Once at least three usable samples exist, the
default relative rule warns when the current match time exceeds twice that
service's recent median. Configure the rule with
`--release-identity-regression-multiplier`,
`--release-identity-regression-min-samples`, and
`--release-identity-regression-history-records`. Missing, malformed, or
insufficient history disables only the relative comparison; the fixed warning
threshold and hard timeout remain in force. Malformed history emits a generic
sanitized warning without exposing file contents. Relative warnings report only
the service, current elapsed time, sanitized median, and calculated threshold.

On success, the command reports sanitized
`uni-api-py_match_elapsed_s=<seconds>` and
`uni-celery_match_elapsed_s=<seconds>` values. These are measured from the
start of each service's identity check until its exact startup line is found;
the command never prints process environments or unrelated journal lines. The
same successful check appends a mode-`0600` JSONL record to
`/var/lib/university-portal/deployment-evidence.jsonl`. Each record contains
the revision, UTC timestamp, API match time, and Celery match time. Releases
using the guarded pull script also record the sanitized redundant-overlay count
and repository-relative paths, or `overlay_audit_status: "warning"` when the
non-blocking audit could not produce trusted evidence. YAML contents, digests,
credentials, and absolute host paths are never stored. Older records without
overlay fields remain valid. Failed or mismatched identity checks append
nothing. The history retains the newest 1,000 successful activations. Each
append takes an exclusive lock and atomically replaces the history with complete
JSONL records only; the history and lock files remain mode `0600`. Readers take
a shared lock, so recent-history retrieval sees one complete retained version
while an append or rotation is in progress.

Retrieve the newest successful activation records through the same command:

```bash
PYTHONPATH=. python deploy/safe_restart_smoke.py \
  --recent-deployment-evidence 10
```

Use `--deployment-evidence-path` only for a deliberately different protected
history location or isolated testing.

## Safe restart smoke command

Before a planned production restart, run the checked-in smoke command:

```bash
install -m 0600 /trusted/handoff/database-refresh-rehearsal.json \
  /etc/university-portal/database-refresh-rehearsal-proof.json
cd /opt/university-portal/backend-py
PYTHONPATH=. python deploy/safe_restart_smoke.py \
  --expected-rehearsal-account-id "$DISPOSABLE_AWS_ACCOUNT_ID"
```

The smoke command checks the rehearsal proof before its first database query.
It fails closed unless the proof reports successful teardown, is no more than
24 hours old, names the expected disposable account, and matches the SHA-256
digest of the exact checked-in rehearsal template. The path can be changed with
`--database-rehearsal-proof`; the age bound can be tightened with
`--max-rehearsal-age-hours` (1–24). `DATABASE_REFRESH_REHEARSAL_ACCOUNT_ID`,
`DATABASE_REFRESH_REHEARSAL_PROOF`, and
`DATABASE_REFRESH_REHEARSAL_MAX_AGE_HOURS` provide equivalent environment
configuration for the production service/deployment environment.

The receipt is not trusted as plain JSON. After teardown, the runner asks a
dedicated asymmetric AWS KMS `SIGN_VERIFY` key in the disposable account to
sign the exact canonical proof payload with `RSASSA_PSS_SHA_256`. The receipt
contains only the KMS key ARN and signature, never an access key, secret key,
session token, presigned URL, or reusable credential. Production verifies the
signature offline with the public key pinned in
`deploy/database-refresh-rehearsal-signers.json`, requires its account and key
ARN to match the configured disposable signer, and rejects the known
production account. The disposable principal needs `kms:DescribeKey` and
`kms:Sign` for only that key. Until the dedicated account and key are
provisioned and their public identity is deliberately added to the signer
registry, production maintenance remains blocked.

The default sample is Torrens' known online-only Bachelor of Applied Business
Marketing (Ducere partnership) page. The command resolves its university by
hostname so database ID reordering cannot target another institution.
`--university-id` and `--course-url` remain available if that checked-in sample
must be replaced deliberately.

The command is read-only first. It aborts unless queued, running, and
awaiting-approval scrape counts are all zero; Git/release identity, both
systemd units, API health, Celery ping, the university, and the sample's HTML
content type are valid. It then queues exactly one targeted ordinary-HTML
sample with resume checkpoints disabled and waits for its persisted DONE event.
Success requires one real attempted course, one policy skip, canonical
`domestic_only=1`, and no staged row.
