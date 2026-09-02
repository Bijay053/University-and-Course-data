# Deploy artifacts

Three files, plus the cutover runbook in `../README.md`.

| File | Where it goes on production |
|---|---|
| `uni-api-py.service` | `/etc/systemd/system/uni-api-py.service` |
| `uni-celery.service` | `/etc/systemd/system/uni-celery.service` |
| `nginx.conf` | `/etc/nginx/sites-available/default` (after backup of current) |

## Record the deployed release

Both application services require
`/root/University-and-Course-data/backend-py/.release.env`. Create it from the
immutable revision being deployed, after checking out that revision and before
restarting either service:

```bash
cd /root/University-and-Course-data

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

After copying the service and nginx files:

```bash
systemctl daemon-reload
systemctl enable uni-api-py uni-celery

smoke_since="$(date --iso-8601=seconds)"
systemctl restart uni-api-py uni-celery
nginx -t && systemctl reload nginx
```

## Release-identity smoke check

Confirm both processes started with the exact revision written above:

```bash
expected="release_revision=$deployed_revision"

for unit in uni-api-py uni-celery; do
  systemctl is-active --quiet "$unit"
  journalctl -u "$unit" --since "$smoke_since" --no-pager |
    grep -Fq "$expected" ||
    { echo "$unit did not report $expected" >&2; exit 1; }
done

echo "FastAPI and Celery reported $deployed_revision"
```

Do not complete the deployment if this check fails. A package without `.git`
is supported as long as its deployment pipeline supplies `RELEASE_REVISION`.
