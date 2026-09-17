set -euo pipefail
cd /opt/university-portal/backend-py
set -a
source .env
source .release.env
source /etc/university-portal/database.env
set +a
export PYTHONPATH=.
target=__TARGET_RELEASE__
test "$(git -c safe.directory=/opt/university-portal rev-parse HEAD)" = 7d13d75ed92c35966493c082705007b5925a8002
.venv/bin/python -B deploy/safe_restart_smoke.py \
  --expected-rehearsal-account-id __EXPECTED_DISPOSABLE_ACCOUNT__

# Pause consumption, then inspect every task state before changing code.
reconciler=""
reconciliation_manifest=""
reconciliation_committed=0
cleanup_release() {
  cd /opt/university-portal
  rollback_failed=0
  if [ "$reconciliation_committed" != 1 ] && [ -n "$reconciler" ] && [ -s "$reconciliation_manifest" ]; then
    if ! sudo -u ubuntu backend-py/.venv/bin/python -B "$reconciler" rollback \
      --manifest "$reconciliation_manifest"; then
      rollback_failed=1
    fi
  fi
  rm -f "$reconciler"
  if [ "$rollback_failed" = 1 ]; then
    echo "Generated config rollback failed; consumers remain paused and manifest is retained at $reconciliation_manifest" >&2
    return 1
  fi
  rm -f "$reconciliation_manifest"
  cd backend-py
  .venv/bin/celery -A app.tasks.celery_app control add_consumer scrape >/dev/null 2>&1 || true
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
sudo -u ubuntu git diff --quiet
sudo -u ubuntu git diff --cached --quiet
sudo -u ubuntu git fetch origin main
test "$(sudo -u ubuntu git rev-parse origin/main)" = "$target"
sudo -u ubuntu git merge-base --is-ancestor HEAD "$target"
reconciler="$(mktemp)"
reconciliation_manifest="$(mktemp)"
sudo -u ubuntu git show "$target":backend-py/deploy/reconcile_generated_configs.py > "$reconciler"
chmod 0644 "$reconciler"
chown ubuntu:ubuntu "$reconciliation_manifest"
sudo -u ubuntu backend-py/.venv/bin/python -B "$reconciler" prepare \
  --repo-root /opt/university-portal --target "$target" \
  --manifest "$reconciliation_manifest"
sudo -u ubuntu git pull --ff-only origin main
test "$(sudo -u ubuntu git rev-parse HEAD)" = "$target"
reconciliation_committed=1
rm -f "$reconciler" "$reconciliation_manifest"

# Report verified runtime overlays that the checked-out tracked recipes now
# fully supersede. The helper contains ordinary audit/report failures and uses
# exit 42 only when Git output positively identifies repository corruption.
overlay_audit_status=0
sudo -u ubuntu backend-py/.venv/bin/python -B \
  backend-py/deploy/post_checkout_overlay_audit.py \
  --repo-root /opt/university-portal || overlay_audit_status=$?
if [ "$overlay_audit_status" = 42 ]; then
  echo "Repository corruption detected after redundant-overlay audit failure; services were not restarted" >&2
  exit 1
elif [ "$overlay_audit_status" != 0 ]; then
  echo "REDUNDANT_CONFIG_OVERLAY_AUDIT_WARNING=failed_non_blocking" >&2
fi

release_env="$(mktemp backend-py/.release.env.XXXXXX)"
printf 'RELEASE_REVISION=%s\n' "$target" > "$release_env"
chmod 0644 "$release_env"
mv -f "$release_env" backend-py/.release.env
cd backend-py
export RELEASE_REVISION="$target"
smoke_since="$(date --iso-8601=seconds)"
systemctl restart uni-api-py.service uni-celery.service
.venv/bin/python -B deploy/safe_restart_smoke.py \
  --release-identity-only --journal-since "$smoke_since" \
  --release-identity-timeout-seconds 30
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
echo "DEPLOYED_RELEASE=$target"