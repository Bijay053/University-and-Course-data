# Deploy artifacts

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
| `prove_database_refresh_alert.py` | Publishes one disposable sanitized database-refresh failure alert and proves repeat suppression |

## Refresh the RDS-managed database credential

`DATABASE_URL` is not a deployment input on production. `.env` holds
non-secret, general application defaults and `.release.env` holds only immutable
release metadata. Both services load `/etc/university-portal/database.env`
last; it is a root-only (`0600`) generated file containing the active database
URL and must never be committed, copied into either repository environment
file, or edited manually.

Deploy `database-secret-rotation-iam.yaml` once with the **exact ARN** of the
existing RDS-managed master secret and the production instance ID. The instance
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

## Safe restart smoke command

Before a planned production restart, run the checked-in smoke command:

```bash
cd /opt/university-portal/backend-py
PYTHONPATH=. python deploy/safe_restart_smoke.py
```

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
