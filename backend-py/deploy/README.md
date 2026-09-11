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
| `database-secret-refresh-rehearsal.yaml` | Isolated, tagged disposable RDS/SSM/Scheduler refresh fixture |
| `rehearse_database_secret_refresh.py` | Explicitly opt-in disposable refresh rehearsal orchestrator |
| `deploy_database_secret_rotation_stack.py` | Revision-fenced deployment and post-update verification for the database rotation stack |
| `prove_database_refresh_alert.py` | Publishes one disposable sanitized database-refresh failure alert and proves repeat suppression |
| `prove_database_refresh_alert_delivery.py` | Temporarily triggers and restores the fixed delivery-failure alarm |

## Refresh the RDS-managed database credential

### Disposable AWS refresh rehearsal

Before changing the production refresh transaction, rehearse it only in a
separate non-production account with two private subnets in different AZs. This
creates a uniquely tagged private PostgreSQL instance whose master secret is
RDS-managed, an SSM-only test host, a dedicated five-minute Scheduler group,
and a local Redis/Celery durable-work probe. A preparation SSM document waits
for cloud-init and SSM readiness, verifies the isolated services, cancels the
`scrape` consumer, and enqueues exactly one rehearsal-only
`scrape.university` task. The scheduled refresh applies the production-like
Celery active-task fence, then resumes consumption after both isolated
`uni-api-py` and real `uni-celery` units restart. Success requires exactly one
durable SQLite execution record and an empty Redis queue. It validates STS
identity and every
mutation target's disposable ownership tag; it refuses configured production
accounts and never prints a secret. It rotates the fixture, requires
Scheduler-origin SSM evidence, verifies the restart and RDS-CA
certificate-verifying `SELECT 1`, proves queued work survived, and deletes the
stack in `finally`.

```bash
export RUN_DISPOSABLE_AWS_DATABASE_REFRESH_REHEARSAL=1
python backend-py/deploy/rehearse_database_secret_refresh.py \
  --expected-account-id "$DISPOSABLE_AWS_ACCOUNT_ID" \
  --production-account-id "$PRODUCTION_AWS_ACCOUNT_ID" --vpc-id "$TEST_VPC_ID" \
  --private-subnet-id "$TEST_PRIVATE_SUBNET_A" \
  --second-private-subnet-id "$TEST_PRIVATE_SUBNET_B" \
  --proof-output runtime-proofs/database-refresh-rehearsal.json \
  --proof-signing-key-id "$DISPOSABLE_AWS_PROOF_SIGNING_KEY_ID" \
  --i-understand-this-creates-disposable-aws-resources
```

The runner reads only `DISPOSABLE_AWS_ACCESS_KEY_ID`,
`DISPOSABLE_AWS_SECRET_ACCESS_KEY`, and optional
`DISPOSABLE_AWS_SESSION_TOKEN`. It never falls back to the production
`AWS_SSM_*` identity or the default AWS credential chain. The non-secret proof
is written atomically only after Scheduler is disabled, the stack is deleted,
the exact run-tag residue search is empty, and the RDS-managed secret is gone.

Provision one persistent asymmetric KMS key in the disposable account with
`KeyUsage=SIGN_VERIFY` and an RSA key spec. Restrict the rehearsal principal to
`kms:DescribeKey` and `kms:Sign` on that exact key. Export only its public key:

```bash
aws kms get-public-key \
  --key-id "$DISPOSABLE_AWS_PROOF_SIGNING_KEY_ID" \
  --query PublicKey --output text |
  base64 --decode |
  openssl pkey -pubin -inform DER -outform PEM \
    -out /tmp/database-refresh-rehearsal-public.pem
```

Verify `aws kms describe-key` reports the expected disposable account, region,
key ARN, `SIGN_VERIFY`, RSA key spec, and enabled state. Then add exactly one
entry containing that account ID, full key ARN, and public PEM to
`deploy/database-refresh-rehearsal-signers.json`. Public-key material is not a
secret. Review this pin as a production trust change; never populate it from a
proof file or caller-supplied account.

Do not use a production account ID, production VPC, or production subnets. The
known production account `905043442097` is rejected immutably as well as by the
required CLI production-account guard. The VPC must be non-default and it and
both private, distinct-AZ subnets must carry
`university-portal:disposable-network=<disposable-account-id>`; each subnet
must have an active NAT gateway default route for package and RDS-CA downloads.
Teardown disables Scheduler first, still attempts stack deletion if that fails,
paginates an exact run-tag residue search, and proves the RDS-managed secret is
gone while retaining both primary and cleanup errors. The
script is deliberately not a normal deployment command and performs no work
without its long opt-in flag.

`DATABASE_URL` is not a deployment input on production. `.env` holds
non-secret, general application defaults and `.release.env` holds only immutable
release metadata. Both services load `/etc/university-portal/database.env`
last; it is a root-only (`0600`) generated file containing the active database
URL and must never be committed, copied into either repository environment
file, or edited manually.

Deploy `database-secret-rotation-iam.yaml` only through the checked-in wrapper,
with the **exact ARN** of the existing RDS-managed master secret and the production instance ID:

```bash
python backend-py/deploy/deploy_database_secret_rotation_stack.py \
  --instance-id "$UNIVERSITY_PORTAL_INSTANCE_ID" \
  --database-secret-arn "$DATABASE_SECRET_ARN" \
  --alert-email "$DATABASE_REFRESH_ALERT_EMAIL" \
  --region ap-south-1
```

For every later update, also pass the revision reported by the preceding
successful deployment:

```bash
python backend-py/deploy/deploy_database_secret_rotation_stack.py \
  --instance-id "$UNIVERSITY_PORTAL_INSTANCE_ID" \
  --database-secret-arn "$DATABASE_SECRET_ARN" \
  --alert-email "$DATABASE_REFRESH_ALERT_EMAIL" \
  --expected-current-revision "$CURRENT_DEPLOYED_TEMPLATE_REVISION" \
  --region ap-south-1
```

For the one-time upgrade of an existing stack created before revision fencing,
run only from the administrator-controlled infrastructure pipeline and opt in
explicitly:

```bash
python backend-py/deploy/deploy_database_secret_rotation_stack.py \
  --instance-id "$UNIVERSITY_PORTAL_INSTANCE_ID" \
  --database-secret-arn "$DATABASE_SECRET_ARN" \
  --alert-email "$DATABASE_REFRESH_ALERT_EMAIL" \
  --adopt-unversioned-stack \
  --region ap-south-1
```

This flag is accepted only when the stack has no `DeployedTemplateRevision`
output and cannot be combined with `--expected-current-revision`. After that
single update creates the output, the wrapper rejects the adoption flag and
requires the exact current revision on every update.

The wrapper takes a conditional lease in the stack's DynamoDB table before it
reads the current revision and holds that lease through final verification. It
also rejects an in-progress stack and an expected revision that does not exactly
match the stack output. Thus two workspaces based on the same older revision
cannot both deploy: one holds the lease, and after it finishes the other fails
its stale-revision check. Success is reported only after the stack output and
the default SSM document version, repository revision, and checked-in command
content hash all match. Do not use a bare
`aws cloudformation deploy` command for this stack. The wrapper does not accept
an alternate stack name or caller-supplied revision. It refuses a dirty
repository and verifies the submitted template byte-for-byte against the
immutable checked-out Git HEAD before contacting AWS. That single verified Git
blob is then reused for hashing, CloudFormation submission, and post-deploy
document verification. Existing operator-set values for deployment user,
instance role, schedule state, and alert deduplication interval are retained
with CloudFormation `UsePreviousValue`; the wrapper changes only its explicit
inputs. Run it only through the administrator-controlled
infrastructure deployment identity and protected source/change-approval
pipeline. Do not grant the routine `DeploymentRefreshPolicy` identity
CloudFormation update or execution-role permissions: an updater that can submit
arbitrary template bodies to this IAM-managing stack could escalate its own
privileges.
The very first stack creation has no lock table yet; CloudFormation's atomic
create-by-name operation serializes that bootstrap. On an existing installation
that predates this safeguard, use the one-time adoption command above; the
administrator-controlled deployment identity must be able to write the fixed
lock item. Routine credential refreshes continue to use the scoped deployment
identity; stack-template updates do not.

The instance
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
The refresh client resolves the default SSM document once, invokes that exact
numeric version, and verifies the same content after the host command succeeds;
an infrastructure update cannot silently switch the command mid-invocation.

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

### Alert-delivery failure handling

Both EventBridge target-delivery failures and asynchronous Lambda execution
failures are retained in the encrypted
`university-portal-database-refresh-alert-dlq` queue after bounded retries.
EventBridge input transformers ensure the Lambda and its asynchronous failure
destination receive only the allowlisted source, detail type, instance ID,
document name, status, and disposable test nonce. An EventBridge target-delivery
DLQ record contains the original AWS SSM command-status event plus delivery
metadata rather than the transformed target input. That AWS event schema
contains command identity/status metadata but never command stdout, stderr,
parameters, environment, or credentials. Tests lock this distinction so the
transformer is not incorrectly treated as protection for the EventBridge DLQ.

The SNS topic and SQS queue use a dedicated rotating customer-managed KMS key.
Its key policy allows only account administration, the database-refresh
EventBridge rule prefix for DLQ encryption, and the database-refresh CloudWatch
alarm prefix for direct alarm publication. The notifier role receives only
`kms:GenerateDataKey` and `kms:Decrypt` on this key.

The `university-portal-database-refresh-alert-delivery-failed` CloudWatch alarm
enters ALARM when the queue has any visible message. A second alarm,
`university-portal-database-refresh-notifier-errors`, detects errors sustained
across two five-minute periods. Both publish directly to the same encrypted SNS
operator topic and contain only static resource/metric metadata. Dead-letter
messages are retained for 14 days for investigation.

After confirming the SNS subscription, prove the secondary alarm-to-operator
path without creating or consuming a real dead-letter message:

```bash
python backend-py/deploy/prove_database_refresh_alert_delivery.py \
  --region ap-south-1
```

The proof refuses to run while the delivery alarm is already in ALARM, sets only
that fixed alarm to a clearly labelled disposable ALARM state, waits until AWS
reports the transition, and restores the exact prior state in `finally`. The
deployment identity can describe and set only this alarm. The state reason and
alarm metadata are static and contain no command output, event payload, or
credentials.

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

`uni-celery.service` uses an idle-aware stop helper. It first stops the named
worker from consuming `scrape`, then checks its reserved, scheduled, and active
tasks in transition-safe order before sending the normal warm-shutdown signal.
If all three are empty, it allows 10 seconds for a clean exit and then removes
only process identities (PID plus kernel start time) captured from that worker
tree, preventing leaked pool/thread processes from consuming the full stop
timeout. If quiescing or inspection fails, or any task exists, it fails safe:
no early cleanup occurs and systemd retains the full 90-second graceful-stop
window. Restarting the unit restores consumption. The API uses its normal
prompt Gunicorn shutdown.

To diagnose stop latency without combining both units into one timing, first
pause the `scrape` consumer and verify `celery inspect active` reports
`- empty -`; then time `systemctl stop` and recover each unit separately. Always
start and health-check a stopped unit before testing the next one.

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
cd /opt/university-portal/backend-py
PYTHONPATH=. python deploy/safe_restart_smoke.py \
  --release-identity-only \
  --journal-since "$smoke_since" \
  --release-identity-warning-seconds 5 \
  --release-identity-timeout-seconds 15
```

The command first requires each live service process environment to contain the
exact full `RELEASE_REVISION`. It then retries each service's exact
`release_revision=<revision>` startup line for up to 15 seconds, allowing normal
Gunicorn/Celery startup delay without accepting a missing or mismatched
identity. Do not complete the deployment if this check fails. A package without
`.git` is supported as long as its deployment pipeline supplies
`RELEASE_REVISION`.

The default warning threshold is 5 seconds and can be changed with
`--release-identity-warning-seconds`. A matching startup line found after that
threshold emits one sanitized warning containing only the service name and
elapsed seconds. The match still succeeds unless it exceeds the independent
`--release-identity-timeout-seconds` hard limit.

On success, the command reports sanitized
`uni-api-py_match_elapsed_s=<seconds>` and
`uni-celery_match_elapsed_s=<seconds>` values. These are measured from the
start of each service's identity check until its exact startup line is found;
the command never prints process environments or unrelated journal lines. The
same successful check appends a mode-`0600` JSONL record to
`/var/lib/university-portal/deployment-evidence.jsonl`. Each record contains
only the revision, UTC timestamp, API match time, and Celery match time. Failed
or mismatched checks append nothing.

Retrieve the newest successful activation records through the same command:

```bash
PYTHONPATH=. python deploy/safe_restart_smoke.py \
  --recent-deployment-evidence 10
```

Use `--deployment-evidence-path` only for a deliberately different protected
history location or isolated testing.

## Safe restart smoke command

Before a planned production restart, run the checked-in smoke command:

```bash
install -m 0600 /trusted/handoff/database-refresh-rehearsal.json \
  /etc/university-portal/database-refresh-rehearsal-proof.json
cd /opt/university-portal/backend-py
PYTHONPATH=. python deploy/safe_restart_smoke.py \
  --expected-rehearsal-account-id "$DISPOSABLE_AWS_ACCOUNT_ID"
```

The smoke command checks the rehearsal proof before its first database query.
It fails closed unless the proof reports successful teardown, is no more than
24 hours old, names the expected disposable account, and matches the SHA-256
digest of the exact checked-in rehearsal template. The path can be changed with
`--database-rehearsal-proof`; the age bound can be tightened with
`--max-rehearsal-age-hours` (1–24). `DATABASE_REFRESH_REHEARSAL_ACCOUNT_ID`,
`DATABASE_REFRESH_REHEARSAL_PROOF`, and
`DATABASE_REFRESH_REHEARSAL_MAX_AGE_HOURS` provide equivalent environment
configuration for the production service/deployment environment.

The receipt is not trusted as plain JSON. After teardown, the runner asks a
dedicated asymmetric AWS KMS `SIGN_VERIFY` key in the disposable account to
sign the exact canonical proof payload with `RSASSA_PSS_SHA_256`. The receipt
contains only the KMS key ARN and signature, never an access key, secret key,
session token, presigned URL, or reusable credential. Production verifies the
signature offline with the public key pinned in
`deploy/database-refresh-rehearsal-signers.json`, requires its account and key
ARN to match the configured disposable signer, and rejects the known
production account. The disposable principal needs `kms:DescribeKey` and
`kms:Sign` for only that key. Until the dedicated account and key are
provisioned and their public identity is deliberately added to the signer
registry, production maintenance remains blocked.

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
