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

Bounded credential restarts must exceed systemd's configured orderly stop
window while remaining inside the five-minute SSM transaction.

**Why:** Production units permit 90-second graceful stops; a 60-second outer
timeout expired during a healthy sequential API/Celery transition.

**How to apply:** Keep the refresh document's restart bound above the unit stop
timeout plus startup margin, and retain the outer 300-second SSM deadline.

The production Nginx virtual host is hostname-scoped, so a bare
`http://127.0.0.1/` frontend smoke request can return 404 even when the public
portal is healthy.

**Why:** A successful frontend build and Nginx reload appeared to fail only
because the verification request omitted the portal hostname.

**How to apply:** Verify frontend releases through the public portal URL or send
the configured Host header to localhost. Read the generated index to discover
the current hashed asset path rather than assuming its prefix format.

The live backend uses its project-local `.venv/bin/python`; neither system
`python` nor the older `venv/bin/python3` path is available. Local Nginx HTTP
smokes may return the expected HTTPS redirect, and abbreviated Git hashes can
be eight characters rather than seven.

Frontend source changes are not deployed by pulling Git or restarting the API
and Celery services. Nginx serves the built Vite output directly.

**Why:** A Fix-dialog close correction existed in production source while
Nginx continued serving an older JavaScript bundle, so the reported UI bug
appeared unfixed.

**How to apply:** After frontend changes, run the portal production build and
verify the asset hash in Nginx's live HTML matches the newly built `index.html`.

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