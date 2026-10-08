# Production release-sample lifecycle acceptance — 2026-10-08

## Outcome: passed without recovery

The user approved **at most one new production smoke sample** for this
verification. The unchanged ordinary command
`backend-py/.venv/bin/python -B deploy/safe_restart_smoke.py`, executed from
`/opt/university-portal/backend-py` with the running worker's environment,
returned zero and reported `safe restart smoke passed`.

This completes the outstanding live acceptance check. It does not rewrite the
[blocked October 2 inspections](release-sample-lifecycle-verification-2026-10-02.md)
as successful runs. The [October 8 reconciliation](reported-release-reconciliation-2026-10-08.md)
resolved the earlier release discrepancy; a fresh inspection found production
had subsequently advanced to the exact revision recorded below.

No deployment, service restart, consumer pause, interruption of business
scrapes, task revocation, queue purge, manual recovery, or infrastructure
provisioning was performed. No source or guard was changed on production.
The normal proof, release-identity, active-job, complete worker-inspection,
ordinary-HTML, DONE-validation, task-result, and final idle guards all remained
enabled. Exactly one new sample was created.

## Live revision and source identity

- Host: `i-03547b132b6aa4ffb`, `university-portal-production`, `ap-south-1`.
- Checkout: `/opt/university-portal`.
- Readiness observation: `2026-10-08T06:33:44Z`.
- Checkout and both running service revisions:
  `4bcb044d9d226eaf7747d806f1f842e2676360aa`.
- Both services were active. API PID `2102391`, active since
  `2026-10-08 05:47:53 UTC`; Celery PID `2102445`, active since
  `2026-10-08 05:47:57 UTC`.
- `git merge-base --is-ancestor fcf621a HEAD` passed. The smoke helper and
  task source had no tracked differences from that checkout.
- Smoke helper SHA-256:
  `c97fd4ec29501819c0f55e000a7fea2c02fa7a04f78b5b011f6c5550b1f4686f`.
- Task source SHA-256:
  `40f2333df425122e4497db50a3973b59295e16a8848237193965df41b9329d81`.
- The preflight was additionally fenced to that exact checkout, helper hash,
  and worker revision before invocation. Its ordinary internal checks also
  verified both service identities. The subsequent evidence collector
  confirmed the same checkout and both service identities.

## Acceptance results

| Requirement | Observed evidence |
| --- | --- |
| Ordinary preflight | Return code 0; `06:34:17.592640`–`06:36:22.880407 UTC` |
| Sample identity | `safe_restart_smoke_2e9db0ad053842a8b4ea68ffd4dea0a1` |
| Database terminal status | `completed`, at `06:35:36.750330 UTC` |
| Persisted canonical DONE | Event `done`, sequence 16, persisted at `06:35:37.062729 UTC`; full payload in linked JSON |
| Expected result | Found 1, skipped 1 (`domestic_only`), staged/imported 0, errors/fetch failures 0 |
| Matching Celery task | `e866f71c-2faf-4826-b95e-067ac177befb`, task `scrape.university` |
| Celery result | `SUCCESS`, `{"ok": true, "id": "safe_restart_smoke_2e9db0ad053842a8b4ea68ffd4dea0a1"}` |
| Complete worker idle | One responding worker, with matching ping/active/reserved/scheduled reply sets; every task list empty |
| Database idle | Active-status counts `{}` |
| Recovery | None; claim count 1, requeue count 0 |
| Startup maintenance | No restart performed; no `scrape.requeue_stale` received/succeeded journal entries in the preflight window |

The deliberate domestic-only skip is the ordinary smoke's expected outcome,
not a failed scrape. `validate_done_payload` also passed independently against
the persisted full payload and the actual staged-row count.

The sample's database worker fields were null after completion. Task identity
was therefore not inferred from those fields: the exact matching Celery
SUCCESS journal entry supplied the task ID, and `AsyncResult` independently
confirmed its terminal state and job ID.

## Complete worker replies

The final read-only collector, observed at `2026-10-08T06:37:40.435485+00:00`
and completed at `06:38:00.866 UTC`, recorded:

```json
{
  "ping": {"uni-celery@ip-172-31-40-159": {"ok": "pong"}},
  "active": {"uni-celery@ip-172-31-40-159": []},
  "reserved": {"uni-celery@ip-172-31-40-159": []},
  "scheduled": {"uni-celery@ip-172-31-40-159": []}
}
```

The ordinary preflight itself had already enforced confirmed idle and matching
sample return before it reported success. The later replies provide an explicit
retained snapshot rather than substituting for or bypassing that check.

## Receipts and evidence handling

All commands used the existing dedicated SSM identity and exact host.

| Command ID | Purpose | Result |
| --- | --- | --- |
| `efb0c170-86ba-45d0-b8a2-048df6cdb933` | Read-only readiness, ancestry, hashes, service identities | Success / 0 |
| `45e19bac-e239-4985-a97f-0b179a1ebccc` | Only ordinary preflight invocation; one sample | Success / 0, empty stderr |
| `9d3f1645-34b3-4422-940d-c5d9a001afab` | Initial supplemental read-only evidence collector | Success / 0, but observer cleanup warning |
| `3ae75fb7-98c5-45db-bba2-9595874f654d` | Corrected supplemental read-only collector, same sample | Success / 0, empty stderr |

The first supplemental collector disposed its final count-query connection
after that query's event loop had closed. It emitted a cross-loop cleanup
warning in the **observer process**, not in the sample or worker. The collector
was corrected to query and dispose within one coroutine and rerun read-only
against the same sample. It created no new job and performed no recovery.
The ordinary unchanged preflight had already completed successfully with
empty stderr before either collector ran. The final clean receipt is the
retained supplemental acceptance evidence.

[Full sanitized acceptance evidence](release-sample-lifecycle-evidence-2026-10-08.json)
contains the complete canonical persisted DONE object, exact terminal task
result, success journal entry, full worker reply maps, and invocation timing.
No credentials, environment-file contents, or connection strings are included.
