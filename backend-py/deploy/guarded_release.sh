#!/usr/bin/env bash
set -euo pipefail

if [ "$#" -ne 3 ]; then
  echo "Usage: $0 <predecessor-full-sha> <target-full-sha> <expected-disposable-account-id>" >&2
  exit 2
fi
predecessor="$1"
target="$2"
expected_disposable_account="$3"
if [[ ! "$expected_disposable_account" =~ ^[0-9]{12}$ ]]; then
  echo "Expected disposable account ID must be exactly 12 digits" >&2
  exit 2
fi

cd /opt/university-portal/backend-py
set -a
source .env
source .release.env
source /etc/university-portal/database.env
set +a
export PYTHONPATH=.
sudo -u ubuntu deploy/release_revision_fence.sh \
  verify /opt/university-portal "$predecessor" "$target"
.venv/bin/python -B deploy/safe_restart_smoke.py \
  --expected-rehearsal-account-id "$expected_disposable_account"

# Pause consumption, then inspect every task state before changing code.
reconciler=""
reconciliation_manifest=""
reconciliation_committed=0
tracked_recipe_manifest=""
overlay_audit_evidence=""
cleanup_release() {
  cd /opt/university-portal
  rollback_failed=0
  if [ "$reconciliation_committed" != 1 ] && [ -n "$reconciler" ] && [ -s "$reconciliation_manifest" ]; then
    if ! sudo -u ubuntu /opt/university-portal/backend-py/.venv/bin/python -B "$reconciler" rollback \
      --manifest "$reconciliation_manifest"; then
      rollback_failed=1
    fi
  fi
  if [ -n "$reconciler" ] && [ -s "$tracked_recipe_manifest" ]; then
    if ! sudo -u ubuntu /opt/university-portal/backend-py/.venv/bin/python -B "$reconciler" restore-tracked \
      --manifest "$tracked_recipe_manifest" ||
      ! sudo -u ubuntu /opt/university-portal/backend-py/.venv/bin/python -B "$reconciler" finalize-tracked \
      --manifest "$tracked_recipe_manifest"; then
      rollback_failed=1
    fi
  fi
  if [ "$rollback_failed" = 1 ]; then
    echo "Release config rollback failed; consumers remain paused and manifests are retained" >&2
    cd /opt/university-portal/backend-py
    if ! .venv/bin/python -B - <<'PY'
from app.tasks.celery_app import celery_app
reply = celery_app.control.cancel_consumer("scrape", reply=True, timeout=10)
assert reply and all(
    "ok" in value for item in reply for value in item.values()
), "Cannot confirm scrape consumer cancellation"
queues = celery_app.control.inspect(timeout=10).active_queues()
assert queues and all(
    all(queue.get("name") != "scrape" for queue in worker_queues)
    for worker_queues in queues.values()
), "Scrape consumer is still active"
PY
    then
      systemctl stop uni-celery.service || true
      test "$(systemctl is-active uni-celery.service || true)" != "active"
    fi
    return 1
  fi
  rm -f "$reconciler"
  rm -f "$overlay_audit_evidence"
  rm -f "$reconciliation_manifest"
  rm -f "$tracked_recipe_manifest"
  cd backend-py
  /opt/university-portal/backend-py/.venv/bin/celery -A app.tasks.celery_app control add_consumer scrape >/dev/null 2>&1 || true
}
trap cleanup_release EXIT
.venv/bin/python -B - <<'PY'
import asyncio
from app.tasks.celery_app import celery_app
from app.database import AsyncSessionLocal
from sqlalchemy import text
reply = celery_app.control.cancel_consumer("scrape", reply=True, timeout=10)
assert reply and all("ok" in value for item in reply for value in item.values()), "Cannot pause consumers"
inspect = celery_app.control.inspect(timeout=10)
import time
for attempt in range(3):
    states = {state:getattr(inspect,state)() for state in ("reserved","scheduled","active")}
    if all(replies and all(not tasks for tasks in replies.values()) for replies in states.values()):
        break
    if attempt == 2:
        raise RuntimeError("Workers did not become idle; release left unchanged")
    time.sleep(5)
async def check():
    async with AsyncSessionLocal() as db:
        count = (await db.execute(text(
            "SELECT count(*) FROM scrape_runtime_jobs WHERE status IN ('queued','running','awaiting_approval')"
        ))).scalar_one()
        assert count == 0, "A scrape started during deployment preflight"
asyncio.run(check())
print("Consumers paused; all workers and jobs idle")
PY
cd /opt/university-portal
sudo -u ubuntu git diff --cached --quiet
reconciler="$(mktemp)"
reconciliation_manifest="$(mktemp)"
tracked_recipe_manifest="$(mktemp)"
sudo -u ubuntu git show "$target":backend-py/deploy/reconcile_generated_configs.py > "$reconciler"
chmod 0644 "$reconciler"
chown ubuntu:ubuntu "$reconciliation_manifest"
chown ubuntu:ubuntu "$tracked_recipe_manifest"
sudo -u ubuntu /opt/university-portal/backend-py/.venv/bin/python -B "$reconciler" prepare-tracked \
  --repo-root /opt/university-portal --target "$target" \
  --manifest "$tracked_recipe_manifest"
sudo -u ubuntu git diff --quiet
sudo -u ubuntu /opt/university-portal/backend-py/.venv/bin/python -B "$reconciler" prepare \
  --repo-root /opt/university-portal --target "$target" \
  --manifest "$reconciliation_manifest"
sudo -u ubuntu backend-py/deploy/release_revision_fence.sh \
  checkout /opt/university-portal "$predecessor" "$target"
sudo -u ubuntu /opt/university-portal/backend-py/.venv/bin/python -B "$reconciler" restore-tracked \
  --manifest "$tracked_recipe_manifest"
sudo -u ubuntu /opt/university-portal/backend-py/.venv/bin/python -B "$reconciler" verify-tracked \
  --manifest "$tracked_recipe_manifest"

# Report verified runtime overlays that the checked-out tracked recipes now
# fully supersede. The helper contains ordinary audit/report failures and uses
# exit 42 only when Git output positively identifies repository corruption.
overlay_audit_status=0
overlay_audit_evidence="$(mktemp)"
chown ubuntu:ubuntu "$overlay_audit_evidence"
sudo -u ubuntu backend-py/.venv/bin/python -B \
  backend-py/deploy/post_checkout_overlay_audit.py \
  --repo-root /opt/university-portal \
  --evidence-path "$overlay_audit_evidence" || overlay_audit_status=$?
if [ "$overlay_audit_status" = 42 ]; then
  echo "Repository corruption detected after redundant-overlay audit failure; services were not restarted" >&2
  exit 1
elif [ "$overlay_audit_status" != 0 ]; then
  echo "REDUNDANT_CONFIG_OVERLAY_AUDIT_WARNING=failed_non_blocking" >&2
fi

release_env="$(mktemp backend-py/.release.env.XXXXXX)"
sudo -u ubuntu /opt/university-portal/backend-py/.venv/bin/python -B "$reconciler" verify-tracked \
  --manifest "$tracked_recipe_manifest"
printf 'RELEASE_REVISION=%s\n' "$target" > "$release_env"
chmod 0644 "$release_env"
mv -f "$release_env" backend-py/.release.env
cd backend-py
export RELEASE_REVISION="$target"
smoke_since="$(date --iso-8601=seconds)"
systemctl restart uni-api-py.service uni-celery.service
.venv/bin/python -B deploy/safe_restart_smoke.py \
  --release-identity-only --journal-since "$smoke_since" \
  --release-identity-timeout-seconds 30 \
  --overlay-audit-evidence-path "$overlay_audit_evidence"
curl --fail --silent http://127.0.0.1:8000/api/health
systemctl is-active uni-api-py uni-celery
.venv/bin/python -B - <<'PY'
import re,subprocess,urllib.request,urllib.parse
nginx = subprocess.check_output(["nginx","-T"],stderr=subprocess.DEVNULL,text=True)
hosts = re.findall(r"^\s*server_name\s+([^;]+);",nginx,re.M)
host = next(h for entry in hosts for h in entry.split() if "." in h and h not in {"localhost","_"} and not h.startswith("*"))
url="https://"+host+"/"
with urllib.request.urlopen(url,timeout=30) as r:
    assert r.status==200
    page=r.read().decode()
assets=re.findall(r'<script[^>]+src=["\']([^"\']+)',page)
assert assets, "No frontend bundle found"
with urllib.request.urlopen(urllib.parse.urljoin(url,assets[0]),timeout=30) as r:
    assert r.status==200 and len(r.read())>100
print("PUBLIC_HTML_AND_ASSET_OK",url,assets[0])
PY
sudo -u ubuntu /opt/university-portal/backend-py/.venv/bin/python -B "$reconciler" finalize \
  --manifest "$reconciliation_manifest"
sudo -u ubuntu /opt/university-portal/backend-py/.venv/bin/python -B "$reconciler" finalize-tracked \
  --manifest "$tracked_recipe_manifest"
reconciliation_committed=1
echo "DEPLOYED_RELEASE=$target"