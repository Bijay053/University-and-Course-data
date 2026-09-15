# Task467 production release-gate repair

This is the sanitized operational record for the nonsecret release-gate
setting repair performed on **2026-09-15**. The work used the dedicated SSM
identity against production instance `i-03547b132b6aa4ffb` in `ap-south-1`.
No credential, secret value, database URL, signature, or proof payload was
printed.

## Safety boundary

- The expected account was obtained independently with the configured
  `DISPOSABLE_AWS_*` STS identity. The verified account was
  `151955775532`; it was checked to be a 12-digit non-production account and
  was never read from `proof.account_id`.
- The existing proof was validated against that independent account before
  reporting readiness.
- No service was restarted, stopped, paused, or reloaded. No Celery consumer
  was cancelled, no scrape was dispatched, no deployment was run, and no
  database row was written.
- Database checks were `SELECT`-only active-job snapshots.

## Nonsecret setting repair

The missing setting was provisioned only in the existing service environment
file:

```text
/opt/university-portal/backend-py/.env
DATABASE_REFRESH_REHEARSAL_ACCOUNT_ID=<independently verified disposable account>
```

The value is intentionally not repeated in command output or process
environment output. The bounded update:

1. took an exclusive lock with a ten-second acquisition bound;
2. refused to overwrite a conflicting existing account setting;
3. wrote a temporary file in the same directory;
4. preserved the original file's owner, group, and mode;
5. flushed the temporary file, atomically replaced the target, and flushed the
   parent directory; and
6. verified that the original content was byte-for-byte unchanged apart from
   the one new setting.

The final repair result was:

```text
config_update=repaired_atomic=yes content_prefix_unchanged=yes permissions_unchanged=yes
env_file_loader=PASS expected_setting_verified
shell_environment_loader=PASS
```

An earlier bounded write did not pass the loader check and was immediately
corrected by the exact-suffix atomic repair above. The final file contains one
valid setting line, with no conflicting or duplicate setting.

Both active services remained `active`; their already-running process
environments intentionally still report the setting as absent because no
restart was performed:

```text
uni-api-py.service   state=active   rehearsal_account_process_env=ABSENT
uni-celery.service   state=active   rehearsal_account_process_env=ABSENT
```

The file and shell loader checks therefore prove the setting is ready for the
next authorized service start without changing the current processes.

## Signed proof gate

The existing proof at
`/etc/university-portal/database-refresh-rehearsal-proof.json` was checked by
the repository's `validate_database_rehearsal_requirement` path with the
independent STS account and the documented 24-hour maximum age:

```text
proof_gate=PASS freshness_signature_template_account=verified
```

The proof file remained mode `0600`. This confirms freshness, the pinned
signature, the checked-in rehearsal-template digest, and the independent
account/key binding. It does not authorize a restart while active jobs remain.

## Natural job progress

The post-repair read-only SSM progress check was command
`be2cd4b5-f351-4df1-ad3b-d5137de07033`, running from
`2026-09-15T01:37:02Z` through `2026-09-15T01:38:09Z`. It waited for
approximately 67 seconds without pausing, cancelling, or dispatching work.
The queue and approval counts stayed at zero, while running work progressed:

| University | Runtime job | First observed | Last observed |
|---|---|---:|---:|
| SEGi University & Colleges | `job_0b6903743490` | `30 / 183` | `41 / 183` |
| Curtin University | `job_02f2e36e4f8b` | `10 / 274` | `28 / 274` |
| University of Waikato | `job_45b42731b819` | `20 / 111` | `33 / 111` |
| University of Auckland | `job_c520e9680953` | `0 / 551` | `3 / 551` |
| University of Otago | `job_4239f2c1c6d1` | `0 / 0` | `0 / 196` |

At the final snapshot:

```text
queued=0
running=5
awaiting_approval=0
```

The jobs were naturally progressing, so the signed idle/restart gate remains
**not ready**. The next operational step is to wait for all five jobs to reach
terminal states, then perform a fresh read-only active-count check. Only after
that check is zero and parent authorization is obtained may the existing
signed idle-restart smoke gate be considered.