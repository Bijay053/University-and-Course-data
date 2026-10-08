# Reported release / production identity reconciliation — 2026-10-08

## Conclusion

The October 2 discrepancy was **revision selection, not a different serving
host**. The retained successful production release command targeted
`8b0fcc0d220bfb40d05cd428d684b53f40f687b5` on
`i-03547b132b6aa4ffb` in `ap-south-1`. It finished at 02:08:40 UTC.
The lifecycle update recorded in the earlier investigation,
`fcf621a99225e213f43c0be3ab6b474944ffeba5`, was committed at 02:43:29 UTC,
after that release. The successful receipt therefore did not certify deployment
of the lifecycle update. The 05:59 and 08:30 observations of the older code
were correct; there is no evidence of a lifecycle-containing checkout on this
host between that release and the October 8 fast-forward.

The situation subsequently changed through **separate production operations**.
On October 8 the same host advanced through `4db82e5`, `371a987`, and
`c86ccfe`. The last guarded release retry has a successful SSM receipt and
an exact `DEPLOYED_RELEASE` marker. Fresh read-only inspection now independently
confirms that checkout, release metadata, API, and Celery all agree on
`c86ccfe9699251f8f817bf532d4f7cf052ae2eb3`, which contains the lifecycle
update. Production is not claimed to match the later workspace HEAD.

This reconciliation performed no deployment, restart, consumer changes, sample
creation, database writes, infrastructure provisioning, or interruption of
business work. Historical commands below were retrieved, not rerun.

## Intended target versus actual release

The original verification required the lifecycle implementation identified in
[the October 2 evidence](release-sample-lifecycle-verification-2026-10-02.md).
That document did not supply a separate exact release command or a full target
revision from the user's deployment confirmation. The retained host command
supplies the actual target: `8b0fcc0d220bfb40d05cd428d684b53f40f687b5`.
It is an older revision, not evidence that the required lifecycle code shipped.
This establishes the technical discrepancy; it does not infer who selected
the target or what an unavailable conversation meant by “deployed.”

The original successful command was
`4e1a9a9d-383e-4ef1-8e77-9b694ab81a75`. Its SSM invocation reports
`Success`, response code 0, start `2026-10-02T02:06:22.134Z`, and end
`2026-10-02T02:08:40.134Z`. Relevant output:

```text
Consumers paused; all workers and jobs idle
Updating 35dedf36..8b0fcc0d
FRONTEND_RELEASE_OK /assets/index-BwieLdDH.js
release identity passed: release=8b0fcc0d220bfb40d05cd428d684b53f40f687b5
DEPLOYED_RELEASE=8b0fcc0d220bfb40d05cd428d684b53f40f687b5
```

Production's retained Git reflog has the October 2 fast-forward at 02:08:23 UTC.
Its next checkout movement is October 8 at 03:13:27 UTC. This agrees with the
earlier readiness observations and with the later release receipts.
The original successful release did finish, but it finished on the wrong
revision for the lifecycle acceptance requirement. No alternate host is
needed to explain that result. The available records do not establish whether
an unrelated operation was also attempted elsewhere.

## Later release records on the same host

All times are UTC on October 8. Full revisions appear in the identity section
or the preceding section; prefixes below are only display abbreviations.

| SSM command | Execution interval | Target | Observed result |
| --- | --- | --- | --- |
| `03a55edd-9aea-4e85-a1c1-3010595fc4e2` | 03:11:25–03:13:47 | `4db82e5` | Failed, code 1, after checkout advanced; worker observation reported no ping replies / observation deadline. Not a completed release. |
| `4ce79f67-0c23-4d2c-a0ed-efe98c083427` | 04:28:50–04:32:24 | `371a987` | Success, code 0; exact release identity and `DEPLOYED_RELEASE` marker; frontend `/assets/index-B21vpJ9w.js` verified. |
| `1ccd3c68-2b48-44f6-95ea-65b7fe8517ec` | 04:47:18–04:50:52 | `c86ccfe` | Failed, code 1, after checkout advanced; guard refused unrelated `scrape.university` work. Not a completed release. |
| `06c3afd2-84cf-46a7-a5f0-3358061b544e` | 04:56:11–05:07:28 | `c86ccfe` | Success, code 0; paused/idle confirmation, exact process identity, frontend `/assets/index-lTwoudBr.js` verification, and final `DEPLOYED_RELEASE` marker. |

The successful retry was already finished before this investigation began.
Its recorded output includes:

```text
Consumers paused; all workers and jobs idle
FRONTEND_RELEASE_OK /assets/index-lTwoudBr.js
release identity passed: release=c86ccfe9699251f8f817bf532d4f7cf052ae2eb3
FRONTEND_RELEASE_OK /assets/index-lTwoudBr.js
DEPLOYED_RELEASE=c86ccfe9699251f8f817bf532d4f7cf052ae2eb3
```

A checkout movement is deliberately not treated as a success receipt: the
two failed post-checkout commands show why both records are needed.
This report records existing release outcomes; it does not retroactively
authorize the operations that produced them.

## Independently observed live identity

Read-only command `a79499b1-e490-49f6-b0a8-d64992d91cec` succeeded with
code 0 at inspection timestamp `2026-10-08T05:10:27Z`.

| Evidence | Observed value |
| --- | --- |
| EC2 instance / name | `i-03547b132b6aa4ffb` / `university-portal-production` |
| Region / public IP | `ap-south-1` / `13.233.73.176` |
| Hostname | `ip-172-31-40-159` |
| Checkout / service working directory | `/opt/university-portal` / `/opt/university-portal/backend-py` |
| Sanitized Git origin | `https://github.com/Bijay053/University-and-Course-data.git` |
| Checkout HEAD | `c86ccfe9699251f8f817bf532d4f7cf052ae2eb3` |
| `backend-py/.release.env` revision | `c86ccfe9699251f8f817bf532d4f7cf052ae2eb3` |
| API main PID / start | `2100968` / 05:06:52 UTC |
| API process `RELEASE_REVISION` | `c86ccfe9699251f8f817bf532d4f7cf052ae2eb3` |
| Celery main PID / start | `2101021` / 05:06:57 UTC |
| Celery process `RELEASE_REVISION` | `c86ccfe9699251f8f817bf532d4f7cf052ae2eb3` |
| Service states | Both active |
| Local `/api/health` | `status=ok`, `service=uniportal-py` |

Read-only command `d93172f1-a9de-44f2-8487-8ed96d19fdab` also succeeded
with code 0. It confirmed:

- Nginx's configured public hostname is `portal.agentsic.com`; its DNS resolves
  to the EC2 public IP above.
- Public `https://portal.agentsic.com/api/health` returned `status=ok` at
  `2026-10-08T05:11:21.246547+00:00`.
- The API's worker startup journal entries at 05:06:54–05:06:55 and Celery's
  startup entry at 05:06:59 name the same full revision as the main processes.
- The retained SSM scripts and invocation receipts identify releases on this
  host, rather than merely a GitHub push or a workspace revision.

The live `safe_restart_smoke.py` contains `_worker_observation` and the
`sample_task_id=task_id` call. Its SHA-256 is:

```text
c97fd4ec29501819c0f55e000a7fea2c02fa7a04f78b5b011f6c5550b1f4686f
```

That exactly matches `git show c86ccfe:backend-py/deploy/safe_restart_smoke.py`
in the workspace. `git merge-base --is-ancestor fcf621a c86ccfe` returned 0.
These checks prove inclusion of the required update, not just a changed
release environment variable.

The same five tracked operator recipe differences recorded on October 2
remain present: `canterbury_1759.yaml`, `law_1902.yaml`, `londonmet.yaml`,
`mdx.yaml`, and `port_2174.yaml`. They were neither copied nor modified.

## Access limitations and evidence handling

The dedicated production SSM identity can retrieve invocations by exact command
ID, but `ListCommands` is denied. Existing on-host SSM orchestration records
were therefore inspected read-only to recover relevant IDs, then those exact
invocations were retrieved. Only selected revision, status, timing, and guard
messages were retained; environment files, credentials, and arbitrary command
contents were not published.

An intermediate inspection, `accd6cb4-0d98-4406-99e5-1781f17974db`, produced
an overlarge response dominated by scheduled credential-refresh records.
Its truncated output is not used as proof of complete command history.
The filtered replacement above and exact invocation lookups supply the
release evidence used here.

## Approval boundary and remaining acceptance work

The earlier report correctly required a **separately approved guarded release**
before retrying the lifecycle verification. At the current observation that
code-deployment prerequisite is satisfied by independently recorded later
releases; no further deployment is needed merely to resolve this mismatch.
No new release or recovery permission was requested or inferred here.

This task does **not** close the separate live sample acceptance check.
The existing smoke-sample approval was bounded to at most one sample; this
investigation created none. Do not create a new sample without accounting for
samples already created by the separate release operations and the exact
approval scope. The original verification record's unrun acceptance rows
remain historical facts, not failures rewritten as later successes.

A subsequent authorized acceptance run must tie terminal database status,
canonical DONE evidence, the matching sample/Celery SUCCESS result, complete
worker idle replies, and ordinary preflight behavior to its exact sample and
deployed revision. Keep the proof, revision, schema, idle, and ownership guards
in `backend-py/deploy/guarded_release.sh` intact. Do not revoke tasks, recover
business work, purge queues, or restart services merely to obtain that evidence.
