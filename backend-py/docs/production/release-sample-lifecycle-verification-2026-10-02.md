# Release-sample lifecycle verification — 2026-10-02

## Outcome: blocked before sample creation

The user separately approved one production smoke sample, with no deployment,
restart, queue changes, or interruption of business scrapes. Read-only readiness
inspection found that the lifecycle update is not deployed. No sample was created.
This is **not** a successful lifecycle verification.

The user subsequently requested task closure. Closure records the blocked
readiness investigation only; it does not certify the live acceptance criteria.
The remaining live verification is deferred until the deployment prerequisite
has separate approval and is satisfied.

## Production evidence

- Host: existing `university-portal-production` EC2 instance,
  `i-03547b132b6aa4ffb`, region `ap-south-1`.
- Inspection timestamp: `2026-10-02T05:59:53+00:00`.
- Checkout HEAD and `.release.env` revision:
  `8b0fcc0d220bfb40d05cd428d684b53f40f687b5`.
- API process PID `1901000`: active since `2026-10-02 02:08:31 UTC`;
  process `RELEASE_REVISION` matches the full revision above.
- Celery process PID `1901053`: active since `2026-10-02 02:08:36 UTC`;
  process `RELEASE_REVISION` matches the full revision above.
- Deployed smoke source has `_wait_for_done`, but no
  `_wait_for_worker_idle` or `_worker_observation`; the
  `sample_task_id=task_id` call occurs zero times.
- Workspace history contains the lifecycle update at `fcf621a` and subsequent
  loop-resource work at `e377aec`. Neither is claimed as deployed evidence.
- Production tracked configuration differences exist in five recipes:
  `canterbury_1759.yaml`, `law_1902.yaml`, `londonmet.yaml`, `mdx.yaml`,
  and `port_2174.yaml`. They were observed, not modified.
- Successful SSM readiness command:
  `6efd8b1c-0e19-4f6e-9e13-14c638c0173a`.
- Successful process-release identity command:
  `bcb1e04c-cded-460c-b3fb-ea47b6cc4426`.

The initial read-only command stopped at a zero-match `grep` under `set -e`,
before its database or worker-inspection portion. The replacement inspection
collected revision/source evidence without executing that portion. An attempt
to retrieve the first command through `ListCommands` was denied; subsequent
commands retained their IDs and used `GetCommandInvocation` directly.

## Acceptance evidence still required

| Requirement | Result |
| --- | --- |
| Exact deployed revision | Recorded, but predates the lifecycle update |
| Sample database status `completed` | Not run |
| Canonical persisted DONE payload | Not run |
| Celery `SUCCESS`, `{ok: true, id: <sample job ID>}` | Not run |
| Complete empty active/reserved/scheduled replies from every worker | Not run |
| Ordinary release preflight reaches confirmed idle without recovery | Not run |
| Brief startup maintenance, if any | No restart performed; not assessed |

## Next prerequisite

A separately approved guarded production release must deploy a reviewed revision
containing the lifecycle update while preserving existing operational/configuration
changes. The sample approval does not authorize that deployment. Once that
prerequisite is satisfied, rerun the ordinary preflight with its proof and idle
guards intact, create at most the approved sample, and capture all acceptance
evidence above. Do not recover a completed sample, revoke tasks, purge queues,
stop business work, or provision infrastructure to make the check pass.