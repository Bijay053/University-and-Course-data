---
name: Production deployment routing
description: Non-obvious routing constraints for deploying this project to its external AWS host.
---

The production checkout can fetch GitHub without having a usable push
credential. When an authorized preservation commit is created there, transfer
the commit to the authenticated workspace as a bounded Git bundle and push
from the workspace rather than copying credentials onto the server.

Production's Alembic ledger can lag schema changes already present. Inspect
the actual schema before replaying a migration chain. An isolated additive
revision can be run through Alembic Operations without falsely stamping its
unexecuted ancestors; retain the original ledger until it is reconciled.

**Why:** A guarded release found the worker-fencing schema present while the
migration ledger still described a much older schema. Blindly upgrading the
whole chain would replay unrelated data and schema transformations.

**How to apply:** Fence the observed ledger, run only the reviewed additive
revision transactionally with bounded lock/statement timeouts, verify its
column type, and do not claim that this reconciles migration history.

**Why:** Committing approved operator recipe edits succeeded on production,
but its push failed because Git could not obtain a username. A bundle retained
the exact commit, parentage and recipe bytes without provisioning another
secret.

**How to apply:** Verify the exact base revision, changed paths and content
hashes first. Export only the preservation commit relative to that base,
fast-forward the workspace to it, and push without force. Do not mistake
temporary stashed baseline bytes for the operator edits being preserved.

Keep supported operational entrypoints outside `.local/`; an ignore exception
does not make that directory safe for deliverable source code.

**Why:** Completion checkpoints removed the guarded release entrypoint even
after an explicit Git commit, leaving a dirty worktree and broken deployment
tests. Restaging or ignore exceptions did not preserve it.

**How to apply:** Put supported scripts in the tracked deployment directory,
update all documented/tested references together, and retain their safety
checks byte-for-byte when relocating them. Reserve `.local/` for generated
runtime evidence and disposable operation wrappers.

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

Tracked operator recipe edits are not generated-file collisions. Never reset
them merely to satisfy the guarded release’s clean-tree check.

**Why:** Production had two intentional tracked YAML edits while the generated
config reconciler correctly handled only untracked generated stubs. A release
would otherwise overwrite or strand those live settings.

**How to apply:** Require an exact allowlist, back up and hash each tracked edit,
temporarily clean only those paths for the guarded pull, and restore/byte-verify
them in an outer failure-safe trap. Prefer a future first-class release guard
over repeating an ad hoc wrapper.

Treat the fetched remote target as authoritative at the release fence, even
after a successful push and local preflight.

**Why:** Concurrent work advanced the remote branch between preflight and the
release transaction. The exact-target guard stopped safely before pull.

**How to apply:** Never bypass the revision mismatch. Integrate the new remote
tip, rerun affected tests on that exact tree, and render a new release target.

Commit multi-file release restoration before deleting any recovery evidence.

**Why:** Sequential backup deletion can fail partway, leaving a rollback
manifest that incorrectly describes a complete backup set and making failure
cleanup unable to distinguish restored state from partial restoration.

**How to apply:** Revalidate every restored source and backup at one commit
boundary, atomically record the committed state, and only then garbage-collect
backups. Treat cleanup after that boundary as best effort.

AWS-RunShellScript starts commands under `/bin/sh` on this host. Wrap release
transactions explicitly in Bash when they use `pipefail` or other Bash-only
features. Under `pipefail`, do not smoke-check journals with
`journalctl | grep -q`: once `grep -q` finds a match it can close the pipe,
causing `journalctl` to exit on SIGPIPE and the successful match to look like a
failed pipeline. Capture the journal first or use a non-short-circuiting grep.

**Why:** A guarded deployment stopped before pulling when `/bin/sh` rejected
`pipefail`; the corrected Bash transaction deployed successfully but its first
release-identity check falsely failed even though the matching startup line was
already present.

**How to apply:** Use an explicit `bash -s` wrapper for SSM release scripts and
make journal-based identity checks compatible with `pipefail`. Treat a failed
smoke assertion as incomplete until service state and exact release lines are
checked directly.

When the defect being released is inside the mandatory pre-pull smoke command
itself, validate with the target revision's smoke implementation without first
installing the target application code. Fetch and verify the exact target
revision, materialize only its smoke command as a temporary file beside the
existing deployment helpers, run it against the unchanged live services, and
remove it before the clean-tree/pull checks. Never weaken or skip the smoke.

**Why:** The deployed guard once failed immediately after observing a completed
sample because its persisted DONE log arrived in the next transaction. Repeating
the old guard reproduced the race and made it impossible to deploy the bounded
polling fix through the normal old-guard-first sequence.

**How to apply:** Use this exception only for a reviewed and tested change to the
guard itself. Keep the normal account/proof validation, target SHA verification,
idle fence, clean-tree check, restart smoke, release-identity proof, and public
health checks unchanged.

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

Routine encrypted-parameter rotations must bind each fixed SSM invocation to
the exact non-secret parameter version returned by its write, serialize host
transactions with an exclusive lock, and verify that version is still current
before reporting success.

**Why:** A single atomic SecureString does not prevent two rotation callers
from overwriting the pointer between write and retrieval, sharing rollback
files, or reporting success for another caller's values.

**How to apply:** Pass only the expected version (never secret data) to a
fixed-purpose document, fetch that version explicitly, lock backup/apply/
verify/rollback as one host transaction, and check current-version equality
both before and after the service smoke test.

Automatic RDS credential refresh should use EventBridge Scheduler for sub-30-
minute checks; State Manager associations do not support a five-minute rate.
Bind the scheduler trust to its exact schedule-group ARN and account.

**Why:** State Manager rejects or cannot honor `rate(5 minutes)`, and a scheduler
role trusted only by the service principal lets unrelated schedules trigger the
fixed production restart document. AWS rejects a trust `aws:SourceArn` scoped
to an individual schedule; Scheduler requires a schedule-group ARN.

**How to apply:** Put the refresh schedule in a dedicated schedule group. Use a
Scheduler universal SSM SendCommand target, scope its execution role to the
exact instance and document, and constrain AssumeRole with both
`aws:SourceAccount` and that dedicated group ARN. Keep unchanged secret
versions restart-free.

The routine production credential-refresh principal must be able to read the
fixed SSM document as well as invoke it. Do not substitute an arbitrary
RunShellScript when that read permission is missing.

**Why:** A rotated database credential left the managed host environment stale.
Both configured SSM credential sets resolved to the same principal, which could
run ordinary host commands but was denied the document read required to pin and
verify the credential-refresh document. The guarded release then correctly
stopped at its read-only database preflight.

**How to apply:** Before a release depends on database credential refresh,
verify that the dedicated refresh identity—not merely a general remote-command
identity—can read and invoke the exact fixed document. Repair that scoped IAM
path through the approved infrastructure pipeline; never bypass document
version verification or manually handle the database password.

RDS-managed master secrets may contain only username and password; credential
refresh must preserve missing endpoint metadata from the root-only
last-known-good connection URL.

**Why:** The live managed secret carried username and password but omitted host,
port, and database name, so requiring endpoint fields made the atomic refresh
roll back.

**How to apply:** Require only username and password from Secrets Manager. Use
endpoint fields when supplied; otherwise parse host, port, and database path
from the already protected rollback URL. Never log either source.

Credential-refresh transactions must pause new scrape claims before the final
idle check; an earlier maintenance-window check is not a sufficient fence.

**Why:** New production scrape requests arrived between an external idle check
and Celery restart, causing warm shutdown to exceed its transaction timeout.

**How to apply:** Cancel the worker's scrape consumer, inspect Celery's active
tasks over Redis for zero running scrape jobs, then replace credentials.
Preserve queued work; service restart restores consumption.

The post-rotation idle fence must not query PostgreSQL with the previous secret.

**Why:** Once RDS promoted the new password, the scheduled refresh was detected
within five minutes but its pre-restart database query failed authentication
before it could install the new credential.

**How to apply:** Fence and inspect active Celery tasks through the broker,
which remains available during database rotation. Do not depend on the stale
database credential anywhere before the new environment is installed.

Production database environment overrides must be lexically last among systemd
drop-ins, not only last in the base unit.

**Why:** A legacy Celery `env.conf` loaded after the base service and silently
overrode the rotated URL even though the checked-in unit listed the managed
database environment last.

**How to apply:** Have the fixed refresh document maintain a `zz-*` service
drop-in pointing to the root-only database environment, then daemon-reload
before restart and compare effective process environments.

RDS TLS verification on production needs AWS's RDS CA bundle in addition to the
host OS trust store.

**Why:** Python's default CA set rejected the live RDS chain as self-signed when
strict TLS was first enabled, even though the same endpoint worked without the
production TLS requirement.

**How to apply:** Fetch the official AWS global RDS bundle over verified HTTPS,
validate it as a CA file, combine it with the system bundle, and set
`SSL_CERT_FILE` for both services. Never disable hostname or certificate checks.

Celery shutdown must distinguish confirmed idle workers from active or
uninspectable workers; API and Celery stop latency must be measured separately.

**Why:** A combined restart hid that API stopped promptly while an idle Celery
prefork tree survived until systemd's stop timeout. An idle check before
quiescing is unsafe because work can start between inspection and shutdown.

**How to apply:** Stop the exact worker's consumption first, then check reserved,
scheduled, and active work before bounded cleanup. Treat failed inspection as
active, preserve the full graceful window for work, verify process start
identity before delayed signals, and check both services after restart.

The production Nginx virtual host is hostname-scoped, so a bare
`http://127.0.0.1/` frontend smoke request can return 404 even when the public
portal is healthy.

**Why:** A successful frontend build and Nginx reload appeared to fail only
because the verification request omitted the portal hostname.

**How to apply:** Verify frontend releases through the public portal URL or send
the configured Host header to localhost. Read the generated index to discover
the current hashed asset path rather than assuming its prefix format.

The live backend uses `backend-py/.venv/bin/python`; neither a repository-root
`.venv/bin/python`, system Python, nor the older `venv/bin/python3` path is
available. Local Nginx HTTP smokes may return the expected HTTPS redirect, and
abbreviated Git hashes can be eight characters rather than seven.

Release-identity journal checks must retry for the bounded service startup
window after `systemctl restart`.

**Why:** Gunicorn became active before its workers emitted their startup
identity lines, so an immediate one-shot journal assertion reported failure
even though both process environments already contained the exact revision.

**How to apply:** Verify the process environment immediately, then poll the
unit journal for the exact full revision for a short bounded interval before
classifying the restart as failed.

Frontend source changes are not deployed by pulling Git or restarting the API
and Celery services. Nginx serves the built Vite output directly.

**Why:** A Fix-dialog close correction existed in production source while
Nginx continued serving an older JavaScript bundle, so the reported UI bug
appeared unfixed.

**How to apply:** After frontend changes, run the portal production build and
verify the asset hash in Nginx's live HTML matches the newly built `index.html`.

Do not assume the production virtual environment includes pytest or that the
application owner can write Python bytecode caches during release verification.

**Why:** A guarded release correctly stopped before restart when pytest was
absent, then a fallback `compileall` check failed because an existing
`__pycache__` directory was root-owned even though the source was valid.

**How to apply:** Run the full focused tests before deployment. On-host, use the
project `.venv/bin/python` with `PYTHONDONTWRITEBYTECODE=1` and `-B` for a
bounded import/syntax smoke; do not use `compileall` as the application owner
unless cache-directory ownership has been verified.

**Why:** An idle-check failed on both stale Python paths, and a successful
release twice stopped on verification-only assumptions: exact short-hash width
and treating the frontend's HTTP→HTTPS redirect as unhealthy.

**How to apply:** Use `.venv/bin/python` for production scripts, compare the
full commit or a deliberate prefix, and smoke the public HTTPS URL with
redirect following (or explicitly accept the local 301).

The production FastAPI service binds to `127.0.0.1:8000`, and its health route
is `/api/health`; root-level `/health`, `/healthz`, and `/openapi.json` return
404 by design because application routers are mounted under `/api`.

**Why:** A successful release build and service restart was initially reported
as a failed smoke check because verification used the development port 8080 and
then tried root-level health paths.

**How to apply:** Smoke the backend with
`http://127.0.0.1:8000/api/health`, then independently verify the public portal
and its generated hashed JavaScript asset return HTTP 200.

The cryptographic restart-proof gate must obtain its expected disposable account
from independently configured deployment/service state. Never derive that
expected account from the proof being validated.

An authenticated STS identity check using the separately provisioned disposable
AWS credentials can establish the missing expected account independently.

**Why:** The signed proof can be valid while the production service has never
been configured with its independent expected account; copying the account
from the proof would defeat that check.

**How to apply:** Verify the dedicated disposable identity, preserve unrelated
environment content and permissions when provisioning the nonsecret setting,
and verify the environment loader accepts it. Existing service processes will
not inherit the new setting until restart; configuring it does not waive the
idle requirement or authorize interrupting active scrapes.

**Why:** A planned release found neither the documented root application
environment file nor `DATABASE_REFRESH_REHEARSAL_ACCOUNT_ID` in the running
Celery process. The checked-in gate correctly failed closed before Git pull or
service restart.

**How to apply:** Validate the proof before changing the release. If the
independent expected-account setting is absent, leave production on the current
revision; if the scrape consumer was paused to drain work, restore and verify it
before ending the attempt.

The production checkout intentionally contains untracked runtime release files,
generated scraper recipes, and operator backups. A deployment must preserve
them rather than requiring an entirely empty `git status`.

**Why:** A valid guarded release was initially blocked by expected untracked
operational files even though the tracked tree and index were clean.

**How to apply:** Require `git diff --quiet` and `git diff --cached --quiet`,
then compare every untracked path with the target commit and abort on a
tracked-path collision. Never clean or reset the untracked files automatically.

When discovering the public portal hostname from Nginx output, match the
`server_name` directive exactly.

**Why:** A loose substring match selected `server_names_hash_bucket_size` as a
hostname after the release itself had already passed.

**How to apply:** Parse only records whose first token is exactly
`server_name`, then exclude `_` and localhost before running public HTML and
asset checks.

Generated-config reconciliation commands in the release transaction must use
the production virtual environment's absolute interpreter path for both
`prepare` and `finalize`.

**Why:** The transaction changes its working directory to `backend-py` before
the finalizer. A repository-relative `backend-py/.venv/bin/python` therefore
resolved as `backend-py/backend-py/.venv/bin/python` only after services had
already restarted, making a healthy release report failure at its final step.

**How to apply:** Do not rely on the release transaction's current directory
for reconciliation or cleanup helpers. Use absolute repository and interpreter
paths throughout the remote script.

Bootstrap releases must use the injected revision-fence path for both the
initial verification and the final checkout fence.

**Why:** An older production revision did not contain the new fence helper. A
temporary target guard passed its first injected-fence check, then failed before
checkout because the production branch hard-coded the target-only helper path.

**How to apply:** Route every revision-fence invocation through the same
overrideable variable. When bootstrapping from a predecessor without the helper,
materialize the reviewed target guard and fence as temporary files and retain
all ordinary smoke, idle, revision, rollback, and public-health checks.

A successful backend release identity does not prove that frontend changes were
built or published.

**Why:** Production reached the target Git revision and both services passed,
but Nginx kept serving an older hashed Vite bundle, so the repair-button fix was
still absent.

**How to apply:** For every release containing portal changes, build the
University Portal on the production checkout and verify the public HTML points
to a newly generated asset containing a target-specific marker before reporting
success.

Atomic frontend directory swaps must make the staged static tree readable and
traversable by Nginx, then gracefully reload Nginx after both publication and
rollback restoration.

**Why:** The build itself was valid, but Nginx retained file metadata for the
old directory inode after it was moved beneath a private rollback directory.
Every public check returned filesystem EACCES until rollback moved that inode
back, which looked like a transient public-edge failure.

**How to apply:** Before the atomic move, explicitly grant read/traverse access
to the static tree. After the move, validate the Nginx configuration and reload
it so workers resolve the new inode. If the release rolls back, reload again
after restoring the prior directory; still require the exact public release
marker before declaring success.
