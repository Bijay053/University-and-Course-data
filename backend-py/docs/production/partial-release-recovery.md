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
