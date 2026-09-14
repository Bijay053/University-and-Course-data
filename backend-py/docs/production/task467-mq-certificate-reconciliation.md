# Task467 Macquarie research-certificate reconciliation

This is an independent, read-only follow-up to the Task466 Macquarie research
catalogue audit. It reconciles the two unresolved handbook certificates using
the official live Macquarie course-handbook records and records the current
signed idle-gate result. No application code, production configuration,
database row, worker, service, or release was changed.

The evidence was captured on **2026-09-14**. The production checks used the
dedicated SSM identity and the existing production host
`i-03547b132b6aa4ffb` in `ap-south-1`.

## Decision

Both unresolved certificates are real, current **2026 official handbook
records**, but neither is an international-student offering in the official
structured course record:

| Certificate | Code | Current official disposition |
|---|---|---|
| Graduate Certificate of Research in Arts | `C000410` | Handbook record is published and approved, but `international_students=false`, no CRICOS code/status is present, and both listed 2026 sessions have `offered=false`. |
| Graduate Certificate of Research in Science and Engineering | `C000405` | Handbook record is published and approved, but `international_students=false`, no CRICOS code/status is present, and both listed 2026 sessions have `offered=false`. |

This resolves the international-catalogue question as **not currently
internationally eligible**, rather than as a missing international course.
It does **not** label either certificate `domestic_only`: the authoritative
record says that international students are false and that the 2026 sessions
are not offered, but it does not make an affirmative domestic-only statement.
That distinction is intentional and avoids inferring domestic-only status from
the default admissions-page rendering.

The certificates therefore should not be added to the international
Funnelback denominator or used as evidence that the production international
catalogue is missing two courses. If a downstream schema requires a binary
filter reason, use the existing source-evidence disposition
`not_international_current_offering`, not an unverified
`domestic_only` inference.

## Official live source evidence

### Source and capture method

The source is Macquarie University's official course handbook, not a
third-party index:

- <https://coursehandbook.mq.edu.au/2026/courses/C000410>
- <https://coursehandbook.mq.edu.au/2026/courses/C000405>

Both pages returned `HTTP 200` with `Content-Type: text/html` during this
capture. The page's server-rendered `__NEXT_DATA__` object was read at
`props.pageProps.pageContent`; the fields below are the provider's structured
course data. This is independent of the Task466 Funnelback response and does
not rely on whether a domestic tab happens to be the default browser view.

The handbook sitemap was also live and identified itself as current:

- <https://coursehandbook.mq.edu.au/sitemap.xml>
- HTTP `200`, sitemap `lastmod=2026-09-07`
- Captured sitemap SHA-256:
  `6b6ad10af7d263fd1c7e21eb35f7a40dfecb82d8f8591a067f4508b67f1d6d94`

The admissions URLs were not used to classify either certificate. Direct
requests to the corresponding `www.mq.edu.au/study/find-a-course/...` routes
returned a Cloudflare `403` challenge from the verification network; that
transport result is not evidence of domestic-only status and was deliberately
not substituted for the official handbook record.

### C000410 — Graduate Certificate of Research in Arts

Source:
<https://coursehandbook.mq.edu.au/2026/courses/C000410>

The live response was `HTTP 200`, `191264` bytes. Response headers reported
`Date: Mon, 14 Sep 2026 07:08:59 GMT`, `x-cache: Hit from cloudfront`, and
`age: 2593`. The captured response SHA-256 was:

`2b00352c5dcb25d1da6a6756fa91661fa977f330136656c6d5e19da09da23000`

Selected structured fields from the official page:

```text
title                         = Graduate Certificate of Research in Arts
course_code                   = C000410
implementation_year           = 2026
effective_date                = 2026-01-01
status                        = Approved / Active
published_in_handbook         = Yes
type                          = Research Graduate Certificate
international_students        = false
cricos_code                   = ""
cricos_status                 = null
cricos_disclaimer_applicable  = false
```

The same record lists two published session entries, both at North Ryde:

```text
Session 1-North Ryde: offered=false, publish=true, language=English
Session 2-North Ryde: offered=false, publish=true, language=English
```

The combination of an exact code/title match, explicit
`international_students=false`, empty CRICOS data, and no offered 2026
session is sufficient to reconcile this record out of the international
catalogue. It is not sufficient to assert that domestic admission is the
only available path.

### C000405 — Graduate Certificate of Research in Science and Engineering

Source:
<https://coursehandbook.mq.edu.au/2026/courses/C000405>

The live response was `HTTP 200`, `229286` bytes. Response headers reported
`Date: Mon, 14 Sep 2026 07:08:59 GMT`, `x-cache: Hit from cloudfront`, and
`age: 2593`. The captured response SHA-256 was:

`b4e3bd2dc7205805a7dcd366b3adf17d53d4808bc0cfa443f2afffe7815855c9`

Selected structured fields from the official page:

```text
title                         = Graduate Certificate of Research in Science and Engineering
course_code                   = C000405
implementation_year           = 2026
effective_date                = 2026-01-01
status                        = Approved / Active
published_in_handbook         = Yes
type                          = Research Graduate Certificate
international_students        = false
cricos_code                   = ""
cricos_status                 = null
cricos_disclaimer_applicable  = false
```

The same record lists two published session entries, both at North Ryde:

```text
Session 1-North Ryde: offered=false, publish=true, language=English
Session 2-North Ryde: offered=false, publish=true, language=English
```

As with C000410, this is an official structured non-international and
non-offered-current-session disposition, not a domestic-only inference.

### Reconciliation consequence

Task466 correctly left both certificates unresolved because a missing
international Funnelback result and domestic-default HTML could not establish
their status. The official 2026 handbook records now supply the missing
authority:

1. Both records exist and are published in the academic handbook.
2. Neither record is marked for international students.
3. Neither has a CRICOS code or CRICOS status.
4. Neither lists an offered 2026 session.
5. Therefore neither is an international-catalogue omission.
6. The evidence does not support creating a domestic-only provider variant or
   adding either record to the staged international course set.

This is a bounded source-category reconciliation. It does not change
`discovery.expected_min_courses`, the MQ YAML, existing staging rows, or the
Task466 conclusion that the 300-course floor must remain in place.

## Read-only production gate inspection

### Commands and safety boundary

The inspection was performed through AWS Systems Manager
`AWS-RunShellScript` using read-only host commands and SQL `SELECT` queries.
The SSM command IDs were:

| Inspection | SSM command ID | Result |
|---|---|---|
| Release, proof metadata, service/process identity, active-count, health, and sample checks | `4e305b70-e0e2-46e3-8bd1-d896043775ab` | `Success` |
| Active-job detail query | `b0b7fef4-4109-48e0-87d5-baa6184a06cd` | `Success` |

The inspection did not run `systemctl restart`, `systemctl stop`,
`systemctl reload`, Celery `cancel_consumer`, a scrape dispatch, a pause, a
database write, a staging write, a deployment, or a repository update. It did
not print database URLs, passwords, access keys, signatures, or process
environment values.

The checked-in gate's ordering and failure behavior are defined in
`backend-py/deploy/safe_restart_smoke.py`:

1. Validate the signed rehearsal proof against an independently configured
   disposable account.
2. Require zero `queued`, `running`, and `awaiting_approval` jobs.
3. Match the release identity and verify both services.
4. Verify API health, Celery ping, and ordinary sample HTML.
5. Only then create and dispatch the targeted sample scrape.

The last step is intentionally not read-only. It was not reached or attempted
in this audit.

### Observed prerequisites

| Gate prerequisite | Read-only result | Consequence |
|---|---|---|
| Release file | Present at `/opt/university-portal/backend-py/.release.env`; `RELEASE_REVISION=b07a55245a7c41886cbb1b15d84c562055195caa` | Pass |
| API process identity | `uni-api-py` active; live process carried the exact release revision | Pass |
| Celery process identity | `uni-celery` active; live process carried the exact release revision | Pass |
| Active jobs | `queued=0`, `running=1`, `awaiting_approval=0` | **Fail: not idle** |
| Running job | `job_2a53f4615018`, SEGi University & Colleges (`university_id=13`), `single`, `current=84`, `total_found=308`, started `2026-09-14 07:21:02.614322+00:00`, release `b07a55245a7c41886cbb1b15d84c562055195caa` | **Fail: must drain before maintenance** |
| Signed rehearsal proof | Present at `/etc/university-portal/database-refresh-rehearsal-proof.json`, mode `0600`; result `passed`, teardown verified, signature algorithm `RSASSA_PSS_SHA_256`; completed `2026-09-14T03:17:02.666013+00:00` (approximately 4.58 hours old at inspection) | Freshness and shape pass |
| Independent expected rehearsal account | `DATABASE_REFRESH_REHEARSAL_ACCOUNT_ID` absent from both live service process environments and checked environment files | **Fail closed: cannot validate proof** |
| Managed database environment | Present at `/etc/university-portal/database.env`, mode `0600` | Pass |
| API health | `HTTP 200`, `{"status":"ok","service":"uniportal-py",...}` | Pass |
| Celery ping | Returned `pong` | Pass |
| Ordinary sample HTML | Torrens sample returned `HTTP 200`, `text/html`, with an HTML document | Pass |

The proof's contents were not trusted merely because the file exists. The
validator was correctly blocked before signature acceptance because the
independent expected account setting was absent. The account must never be
derived from `proof.account_id`.

### Release decision

**Release/restart is not currently possible under the signed idle gate.**

There are two independent blockers:

1. `job_2a53f4615018` is still `running`; the queue is not idle.
2. The independently configured `DATABASE_REFRESH_REHEARSAL_ACCOUNT_ID` is
   absent, so the KMS-signed rehearsal proof cannot be validated fail-closed.

The release identity, systemd service state, API, Celery, sample HTML, and
proof age are positive evidence, but none overrides either blocker. Do not
restart, pause, cancel, or dispatch work to force the gate through. Re-check
the active counts after the job has naturally drained, then provision the
expected disposable account through the trusted deployment/service
configuration path and validate the existing proof against that independent
value.

## Exact existing commands for a later safe re-check

The following are the checked-in runbook commands, adapted to the live
production virtual environment (`/opt/university-portal/backend-py/.venv/bin/python`).
They are recorded for the operator; they were not used to bypass the blockers.

### Release identity check

This existing bounded check verifies process and startup release identity. It
also appends sanitized timing evidence on success, so it is not a strictly
no-write inspection even though it does not restart services or write the
database:

```bash
cd /opt/university-portal/backend-py
PYTHONPATH=. .venv/bin/python -B deploy/safe_restart_smoke.py \
  --release-identity-only \
  --journal-since "$smoke_since" \
  --release-identity-warning-seconds 5 \
  --release-identity-regression-multiplier 2 \
  --release-identity-regression-min-samples 3 \
  --release-identity-regression-history-records 10 \
  --release-identity-timeout-seconds 15
```

The current audit instead checked the exact process identity directly through
read-only `/proc` and `systemctl show`, avoiding that timing-evidence append.

### Full safe-restart smoke command

This is the existing command from `backend-py/deploy/README.md`. It must not
be run until the active-job count is zero and the independent expected account
has been provisioned. It installs the proof and, after all preflight checks
pass, dispatches exactly one targeted policy-skip sample; it is therefore not
a read-only command:

```bash
install -m 0600 /trusted/handoff/database-refresh-rehearsal.json \
  /etc/university-portal/database-refresh-rehearsal-proof.json
cd /opt/university-portal/backend-py
PYTHONPATH=. .venv/bin/python -B deploy/safe_restart_smoke.py \
  --expected-rehearsal-account-id "$DISPOSABLE_AWS_ACCOUNT_ID"
```

The command must not be replaced with a bare `systemctl restart`, a worker
pause, a manually supplied proof account, or a proof copied from the proof
payload. After the two blockers are cleared through the approved operational
path, the existing smoke command is the safe release gate and must be allowed
to fail closed if any prerequisite changes.
