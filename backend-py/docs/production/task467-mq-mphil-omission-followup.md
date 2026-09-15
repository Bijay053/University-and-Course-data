# Task467 Macquarie MPhil omission follow-up

This follow-up records the bounded diagnosis and minimal repair for the
Macquarie production run `job_e8ecd007ab8a` on release
`7d13d75ed92c35966493c082705007b5925a8002`.

## Observed omission

The run reached `completed_with_warnings`, with:

- `190` raw and extractable candidates;
- `189` staged rows, all still `pending`;
- one staged `Doctor of Philosophy` row;
- no staged `Master of Philosophy` row;
- the unchanged catalogue floor of `300`.

The persisted targeted runtime-log query (SSM command
`15e2812c-ab59-4c67-ae5b-83556d10b2f9`) recorded:

```text
[DISCOVER] MQ: research authority did not verify Master of Philosophy;
leaving it out rather than staging an unverified row
```

This is a safe omission, not evidence that the qualification is absent from
Macquarie's authority source.  A bounded live probe (SSM command
`b16c760c-84ee-41bf-a78b-c58b12851cf8`) observed a transient rendered
provider failure for the PhD source (`ROTATION_FAILED`, HTTP 502) and a
successful 20,392-byte MPhil source with valid authority evidence.  Direct
origin requests to both research pages returned a Cloudflare challenge (HTTP
403), so direct HTTP is not a valid production authority transport.

## Minimal repair

`_fetch_mq_research_source` used `fetch_html_scrape_do(..., max_retries=0)`.
Consequently, one transient rendered-provider failure could make one
qualification fail closed while the other qualification was staged.  The
helper now requests exactly one bounded provider retry
(`max_retries=1`).  It still:

1. rejects challenge shells;
2. falls back to direct HTTP only as a secondary transport;
3. requires exact qualification title, full-time duration, and
   international-student evidence;
4. requires the central research-fee authority before staging a metadata-only
   row; and
5. never copies a domestic/default fee into `international_fee`.

The 300-course catalogue floor and its warning/terminal guard are unchanged.

## Regression

`tests/test_mq_research_authority.py` now asserts that the authority fetch
requests one provider retry while preserving the existing exact-title,
duration, international-evidence, route-identity, fee-safety, and two-row
staging regressions.

Focused local regression run on the merged primary worktree:

```text
137 passed, 1 skipped
tests/test_mq_research_authority.py
tests/test_mq_coursehandbook_sitemap.py
tests/test_mq_coursehandbook_to_admissions.py
tests/test_stage_evidence_and_review.py
```

## Required post-deploy evidence

After this repair is deployed, one fresh `forceDiscovery=true` MQ run must
verify both rows in the exact current review set, including rows deliberately
retained through the run's persisted resume checkpoint:

- `Doctor of Philosophy`: `Doctorate`, `3.00 year`, `Full Time`;
- `Master of Philosophy`: `Master's`, `2.00 year`, `Full Time`;
- both rows `status=pending`;
- both rows with selected authority evidence for title, duration, full-time,
  international evidence, and the central fee source;
- both rows with `international_fee=NULL` unless an explicitly labelled
  international amount is returned by the current-route page-data; and
- the catalogue floor remains `expected_min_courses=300`, even if the run
  remains `completed_with_warnings` below that floor.

No approval, deletion, baseline reduction, or unrelated stale-row reuse is part of this
verification.

## Follow-up release gate attempt

The corrected primary head was pushed normally after an origin recheck:

```text
origin/main=bb2c24b
```

Focused local regressions passed before the push.  The signed idle gate was
then invoked with the independently verified rehearsal account and failed
closed before any deployment or restart:

```text
safe restart smoke aborted: active scrape jobs prevent restart smoke:
queued=0, running=2, awaiting_approval=0
```

The bounded read-only active-job detail command
`e2a5224b-0d66-4958-b2ad-041a225aa48c` found:

```text
job_ab89ec575175  University of Auckland  running  282/551
job_f1e69a13338d  University of Otago    running  194/194
```

Production therefore remains on the prior deployed release
`7d13d75ed92c35966493c082705007b5925a8002`; no restart, deployment, or fresh
MQ scrape was attempted while the gate was blocked.  The next attempt must
repeat the signed idle gate after these jobs reach terminal states.  It must
not bypass the gate or start a second corrective scrape while this one is
blocked.

## Final pinned deployment verification

The signed idle gate was later re-run after the active jobs drained.  The
approved deployment used only Task467 release
`6ef87d880001d98bf533f62f2fb01100f70ab919`; unrelated origin work was not
deployed.  Sanitized verification evidence is retained under these SSM
records:

- signed idle/rehearsal gate: `3e2fd6dc-9f78-41e3-a8f2-2fbbd8a4ce75`;
- safe smoke proof: `safe_restart_smoke_9aa1f9fc234a400592dda046aebe89b4`;
- guarded deployment/restart: `e21182d0-64de-4990-8b9f-517e88d698f7`;
- release identity, API health, and Celery ping: `1ede7d80-4be7-466d-9657-2c0aab967acb`;
- public HTML and hashed JavaScript asset verification:
  `00fd3f8a-d376-4c0e-8811-f951f2116172`.

The deployment proof recorded a clean tracked worktree, preservation of 27
untracked runtime files, active `uni-api-py` and `uni-celery` services, and
valid/reloaded nginx configuration.  Release identity and API/Celery checks
passed for the pinned full revision.  `https://portal.agentsic.com/` returned
HTTP 200, and the verified hashed JavaScript asset also returned HTTP 200.
The disposable database-refresh rehearsal was independently signed and
verified with `teardown_verified=true`; the independent residue check found
zero non-terminated disposable resources and no matching CloudFormation
stack.

## Fresh MQ run: terminal acceptance evidence

Exactly one fresh `forceDiscovery=true` Macquarie run was dispatched after the
pinned deployment:

- dispatch record: `c52321b1-d506-4214-abbc-85e3e857ee0c`;
- university/database ID: Macquarie University / `25`;
- runtime job: `job_23fe4e59ef3f`;
- terminal status: `completed_with_warnings`;
- terminal counters: `196` found, `195` imported/staged, `1` skipped
  (`online_only`), `0` fetch failures, and `0` errors;
- terminal time: `2026-09-15 04:45:19 UTC`.

The persisted discovery accounting was:

```text
372 Funnelback records
→ 209 valid canonical current-route URLs
→ 194 enrichment candidates after 15 dual origin-not-found exclusions
→ 179 full page-data records + 17 metadata-only records
→ 196 validated links after the two research-authority rows
→ 195 staged rows after one online_only skip
```

The terminal events also preserved `expected_min_courses=300` in the DONE
and catalogue-floor warning records.  The floor was not lowered: the run
correctly remained below the floor with a warning.  Canonical current-route
handling removed year aliases before enrichment; the terminal log does not
persist a separate combined-degree subtype counter, so no combined-route
sub-count is inferred from the 372 raw records.

Both authority qualifications were verified during discovery.  The
`Master of Philosophy` row is owned by the fresh job and passed the required
checks:

- exact title, `Master's`, `2.00 year`, and `Full Time`;
- `status=pending`;
- selected authority evidence for title/classification, duration, full-time,
  international eligibility evidence, and the central research-fee source;
- `international_fee=NULL`, with no fee term or currency and
  `has_central_fee_page=true`.

Doctor of Philosophy was retained through the fresh job's explicit resume
checkpoint. The checkpoint skipped 183 already-staged rows
(persisted in its request payload as `resumeCourseIds` and
`resumeSourceJobIds`).  The PhD row that was counted in the terminal
`imported=195` total is row `29322` from the earlier
`job_e8ecd007ab8a`, not a row owned by `job_23fe4e59ef3f`.  It is still
`pending` and independently has the expected `Doctorate`, `3.00 year`, `Full
Time`, selected authority evidence, central-fee evidence, and
`international_fee=NULL`.

No row was approved, deleted, or manually repaired.  No second MQ scrape was
started. The fresh run verified both research authorities on the corrected
release; MPhil was newly staged, while PhD was intentionally preserved in the
same bounded review set through the persisted checkpoint IDs. This is expected
resume behavior, not reuse of an unrelated stale row, and avoids duplicating or
deleting a valid pending course.

Task467's production acceptance therefore passes across the exact resume-chain
review set. The catalogue floor remains 300 and was not reassessed downward:
one warning-bearing run with 196 validated links is not sufficient authority
to lower it.

This evidence records the pinned Task467 production release only. Later,
unrelated changes on the repository's main branch were not included in that
deployment.
