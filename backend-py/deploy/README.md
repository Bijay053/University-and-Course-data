# Deploy artifacts

Three files, plus the cutover runbook in `../README.md`.

| File | Where it goes on production |
|---|---|
| `uni-api-py.service` | `/etc/systemd/system/uni-api-py.service` |
| `uni-celery.service` | `/etc/systemd/system/uni-celery.service` |
| `nginx.conf` | `/etc/nginx/sites-available/default` (after backup of current) |

After copying:
```
systemctl daemon-reload
systemctl enable --now uni-api-py uni-celery
nginx -t && systemctl reload nginx
```
