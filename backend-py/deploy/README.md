# Deploy artifacts

Three files, two secure OpenAI rotation tools, plus the cutover runbook
in `../README.md`.

| File | Where it goes on production |
|---|---|
| `uni-api-py.service` | `/etc/systemd/system/uni-api-py.service` |
| `uni-celery.service` | `/etc/systemd/system/uni-celery.service` |
| `nginx.conf` | `/etc/nginx/sites-available/default` (after backup of current) |
| `install_openai_fallback_via_ssm.py` | Run from a trusted deployment workspace; do not copy to production |
| `rotate_openai_fallback_via_parameter_store.py` | Routine credential rotation from a trusted deployment workspace |
| `openai-parameter-store-iam.yaml` | One-time least-privilege IAM and KMS setup |

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
