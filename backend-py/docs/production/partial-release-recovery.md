# Operator-controlled recovery before services restart

Use this only when an application release advanced the checkout to an exact
target commit, but both running systemd services and `.release.env` still
identify the exact predecessor. A failed frontend build is one such case.

This tool restores a truthful **predecessor baseline**. It never installs
packages, writes release metadata, restarts the API, publishes frontend files,
runs ordinary scrape samples, or declares a deployment complete.

## Required checks

- Distinct full 40-character predecessor and target IDs are local and related
  by ancestry; no lazy fetching, Git replacement objects or moving-ref policy.
- Both the API and worker processes identify the predecessor, and the loaded
  and stored release metadata agree.
- The locally published and publicly served frontend both prove the predecessor
  release marker. Frontend publication happens before metadata and service
  restart; those old identities alone cannot prove the old frontend is live.
  Missing, unknown or target frontend identity requires explicit operator
  frontend recovery; this tool never guesses which rollback directory is safe.
- The existing signed disposable-account rehearsal is valid and fresh.
- Complete worker inspection and database counts prove no queued, running or
  awaiting-approval business jobs. Existing consumers must initially be enabled.
  Queue-inspection replies must match the complete ping/task snapshot exactly;
  a timeout returning only some workers is never accepted as the worker set.
- The original failed/interrupted release has a valid **original preflight**
  receipt and no post-restart phase. Completed or post-restart attempts are
  outside this tool's scope.
- Only regular, tracked university recipe files may have operator edits.
  Staged changes, other source edits, and untracked/ignored checkout collisions
  refuse recovery.

Do not replace `.release.env` with the target revision to make a smoke pass.
Do not reset, clean, stash or force checkout to remove a blocker.

## Stage immutable tooling outside the checkout

Recovery must survive changing the checkout back to a predecessor which may not
contain this tooling. As the production operator, stage these reviewed target
files in a new root-private directory **before** invoking recovery:

```bash
cd /opt/university-portal
sudo bash
set -euo pipefail
umask 077

# Set these to the reviewed attempt; do not infer them from a moving branch.
PREDECESSOR="<exact predecessor SHA>"
TARGET="<exact target SHA>"
FAILED_MANIFEST="/var/lib/university-portal/release-manifests/release_<id>/manifest.json"
ACCOUNT="<independently verified disposable rehearsal account>"

TOOL="$(mktemp -d /var/lib/university-portal/recovery-tool.XXXXXX)"
for FILE in recover_partial_release.py partial_release_recovery.py \
            partial_release_host.py release_manifest.py verify_frontend_release.py; do
  sudo -H -u ubuntu git -C /opt/university-portal \
    show "$TARGET:backend-py/deploy/$FILE" > "$TOOL/$FILE"
done

cd /opt/university-portal/backend-py
set -a
source .env
source .release.env
source /etc/university-portal/database.env
set +a
export PYTHONPATH=. PYTHONDONTWRITEBYTECODE=1

.venv/bin/python -B "$TOOL/recover_partial_release.py" check \
  --predecessor "$PREDECESSOR" --target "$TARGET" \
  --failed-manifest "$FAILED_MANIFEST" \
  --expected-rehearsal-account-id "$ACCOUNT"
```

All named target objects must already be provisioned and reviewed. A predecessor
without the tool cannot use a later tool that is absent from its intended target
merely by changing the target argument: pin and separately review the bootstrap
tool revision before any such backport.

`check` observes services, queues, proof and Git only. It does not pause
consumers, change the checkout or write a recovery journal. It briefly takes the
same root-owned exclusive lock as `guarded_release.sh`; do not run competing
legacy release commands that lack that lock.

## Execute recovery explicitly

After reviewing a successful check, use the same environment and arguments:

```bash
.venv/bin/python -B "$TOOL/recover_partial_release.py" recover \
  --predecessor "$PREDECESSOR" --target "$TARGET" \
  --failed-manifest "$FAILED_MANIFEST" \
  --expected-rehearsal-account-id "$ACCOUNT"
```

The command prints `RECOVERY_STATE=.../state.json` before any consumer change.
It archives original acceptance bytes separately with fsynced files and
non-overwriting atomic publication. It verifies the entire archive and durably
checkpoints completion before changing any consumer. It records worker membership and
recipe hashes, pauses the original consumers and rechecks idleness. It uses the
target's pinned recipe reconciler to retain byte-exact operator backups, makes
a **non-forced detached checkout** of the predecessor, restores and verifies the
operator recipes, then restores and verifies the original enabled consumers.
It verifies the predecessor frontend again while paused, before re-enabling
consumers, and before reporting success. If the frontend changed, recovery
refuses completion and keeps consumers paused for the operator.
The `main` branch is not rewritten. Original release manifests and receipts are
never modified or adopted as a new acceptance phase.

Success reports `baseline_restored` and `release_completed: false`. Only then
may an operator separately start the ordinary guarded release with a **new**
acceptance manifest and fresh ordinary preflight and post-restart samples. The
normal release still owns frontend publication, service restarts and all
release checks. Recovery itself does not publish.

## Interruptions and failure

- On caught interruption, recipe restoration is attempted only when exact
  captured bytes and the journal prove it safe. A concurrent operator edit is
  never overwritten to make cleanup pass.
- A failed recovery leaves consumers paused. A failed or partially acknowledged
  consumer restoration is cancelled again and verified. If containment cannot
  be verified, the local worker service is stopped and verified inactive; the
  API is not stopped. Do not automatically restart that worker.
- Abrupt process death leaves an fsynced checkpoint. Review its state, then
  explicitly invoke `resume` with the same revision/account/manifest arguments
  and `--state` pointing to the printed journal. It completes missing archive
  copies from the unchanged original bytes and refuses to overwrite any
  mismatched archived copy. It revalidates evidence,
  metadata, identities, idleness, worker membership and recipe integrity before
  continuing. A resumed prepare uses a new recipe journal and never overwrites
  an earlier one.
- `worker_stopped`, `containment_failed`, a half-renamed recipe quarantine,
  changed metadata, modified original evidence or unknown concurrent edits
  require operator investigation. `resume` does not restart services or accept
  an unverified state. Backups and journals remain private and are not pruned.

Do not rerun a smoke sample to manufacture proof for the original failed
attempt. The archived evidence is for audit and recovery, not new acceptance.

## Hermetic verification

```bash
cd backend-py
PYTHONPATH=. python -m pytest \
  tests/test_partial_release_recovery.py tests/test_release_manifest.py \
  tests/test_deploy_release_identity.py \
  -q -c /dev/null --noconftest -p no:cacheprovider
```

The recovery suite uses disposable local Git repositories and fake service
controls, including actual process death at durable checkpoints. It does not
contact AWS, use production credentials, alter the workspace database or
perform a production recovery.

## Opt-in production-style Linux ownership tests

`tests/test_partial_release_permissions.py` additionally exercises the real
root CLI entry point, its private umask/journals, the actual `sudo -H -u` handoff,
Git checkout, and the pinned recipe reconciler together. Only service/frontend
observations and consumer controls use the shared fake adapter; no systemd,
Celery, database, network, AWS or production services are accessed. The CLI's
production `ubuntu` account selection is replaced **in the test only** with a
new unique `upr_<random>` account. No production CLI bypass flag is added.

Run this **only on an expendable Linux test host**, not on the production host
or a shared development machine. Prerequisites:

- Genuine root with permission to create/delete local users and groups, and
  noninteractive real sudo from root to the newly created account. A sudo shim
  which leaves the UID unchanged is not sufficient.
- `git`, `sudo`, `useradd`, `userdel`, `groupadd`, `groupdel`, and `setfacl`
  installed. Use a local filesystem supporting Linux POSIX ACLs and the
  reconciler's atomic no-replace rename/hard-link operations.
- Python 3 with pytest and PyYAML installed. The same Python executable and
  PyYAML must be accessible to the disposable account after sudo resets the
  environment. A Python installation below a root-private home is unsuitable.
- A writable `/tmp` with space for small disposable Git repositories. No
  existing `ubuntu` account, production checkout, production credentials,
  service unit, sudoers change or application environment file is needed.

From the test checkout (adjust the path to that disposable checkout):

```bash
cd /root/University-and-Course-data && cd backend-py && \
  sudo -n env ALLOW_DISPOSABLE_RECOVERY_PERMISSION_TESTS=1 PYTHONPATH=. \
  python3 -m pytest tests/test_partial_release_permissions.py \
  -v -c /dev/null --noconftest --import-mode=importlib -p no:cacheprovider
```

Without the opt-in variable all permission cases skip before creating accounts
or files. With opt-in, missing root/tools or unusable sudo/Python/ACL support
**fail**, rather than silently presenting skipped cases as permission proof.
The normal hermetic recovery suite remains runnable without privileges.

The tests verify:

- `check` does not create a recovery journal or helper workspace.
- Root-owned `0700` journal/archive directories and `0600` files deny both
  reads and writes by the application owner, using actual owner-side opens.
- Helper workspaces, recipe journals, backups and restored recipes belong to
  the application UID/GID. Backups and restores retain exact UTF-8/CRLF bytes
  and `0600`, `0640`, and `0644` recipe modes across checkout and resume.
- Owner mode-bit denials and real named-user ACL denials at helper creation,
  backup creation and restoration boundaries produce a nonzero CLI result,
  never report baseline success, and preserve private evidence and backups.
  Separate restoration cases exercise a real write denial and a concurrent
  owner edit, including a refused subsequent resume.

Each run creates only a new `/tmp/portal-recovery-permissions-*` tree and a
unique account/group. Ownership changes are limited to that tree; ACL/mode
denials are applied only to its fixture directories and restored in `finally`.
Fixtures remove their directories and delete the exact account/group, including
after assertion failures. They never use `userdel -r`, recursive permission
repairs, existing service accounts, `/run` release locks, or production paths.

SIGKILL or host loss can prevent fixture cleanup. On that disposable host,
inspect `/tmp/portal-recovery-permissions-*` and `getent passwd` to match an
exact `upr_...` account's home to the exact generated tree; confirm that no test
process is running. Delete **only that verified account**, its matching group,
and that exact temporary tree. Do not use a wildcard deletion or remove an
account merely because its name starts with `upr_`. A successful unprivileged
run with skips is not evidence that the privileged ownership checks passed.
